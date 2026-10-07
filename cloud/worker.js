/**
 * modelookup 账号服务 — Cloudflare Worker + KV
 * =============================================
 * 管理号/子账号 + 逐项权限。所有数据存 KV，免费额度足够用。
 *
 * 部署（dashboard 手动版）：
 *   1. CF 控制台 → Workers & Pages → Create Worker → 名字 modelookup-auth → Deploy
 *   2. 把本文件全部代码粘贴进编辑器覆盖 → Deploy
 *   3. Storage & Databases → KV → Create namespace → 名字 modelookup-users
 *   4. Worker → Settings → Bindings → KV Namespace：变量名 KV，绑上面的 namespace
 *   5. Worker → Settings → Variables → 添加：
 *        ADMIN_USER = boss            （管理号用户名）
 *        ADMIN_PASS = 你的管理密码     （明文，部署后登录会自动转哈希）
 *        SETUP_CODE = 一串随机字符     （首次初始化用，之后可删）
 *   6. 访问 https://modelookup-auth.<你的子域>.workers.dev/ 出现 ok 即部署成功
 *
 * API（全部 JSON）：
 *   POST /login          {u, p}                → {ok, token, role, perm, name}
 *   POST /init           {code}                → 首次初始化（建管理号）
 *   GET  /perm?u=xxx     Header: X-Token       → {ok, role, perm}   （客户端每次启动校验）
 *   POST /changepw       {old, new}            → 改自己密码
 *   GET  /admin/list     Header: X-Token       → {ok, users:[{u, note, disabled, perm}]}
 *   POST /admin/save     {users:[...]}         → 批量保存子账号（权限/备注/停用/新建/改密）
 *                                                管理号 token 才能调。
 */

const PERM_KEYS = [
  "live",        // 库存明细行（各仓）
  "sum",         // 汇总行（正品/样机/次品仓几台）
  "nat",         // 发往（可发区域）
  "code",        // 物料编码行
  "subsidy",     // 补贴栏（69码/售价）
  "xianyu",      // 闲鱼行（ID / 闲鱼价 / 利润）
  "calc",        // 闲鱼定价计算器 + 标记已发布 + 复制
  "p_mh",        // 拿货价·慧买卖（美的系，也是计算器成本下拉项）
  "p_m2",        // 给二批价（美的系）
  "p_mr",        // 价格取整（美的系）
  "supplier",    // 发货 / 货源（隆阳电器）
  // 价格格逐格权限（2026-10 五次改版：管理号一格一格决定给不给看）
  "p_pt",        // 价格含税（也是计算器成本下拉项）
  "p_np",        // 滞销价格（也是计算器成本下拉项）
  "p_ny",        // 样机价格（也是计算器成本下拉项）
  "p_lpz",       // 滞销清理价（也是计算器成本下拉项）
  "p_spz",       // 样机清理价（也是计算器成本下拉项）
  "p_lp",        // 零售指导价
  "p_mp",        // 官网价
  "p_jd",        // 京东价 / 国补后
  "p_lb",        // 滞销出库奖励
  "p_sb",        // 样机出库奖励
  "p_qty",       // 清理数量
  "upd_kdocs",   // 「从金山文档更新数据」按钮
  "upd_yy",      // 「更新实时库存（用友）」按钮
  "addimg",      // 补图 / 导出比价图
];

// 旧权限键 → 新键映射（v2.5 之前的 perm 只存 guide/clear_price 两个粗键）
const PERM_MIGRATE = {
  guide:       ["p_pt", "p_lp", "p_mp", "p_jd"],
  clear_price: ["p_np", "p_ny", "p_lpz", "p_spz", "p_lb", "p_sb", "p_qty"],
  meidi_cost:  ["p_mh", "p_m2", "p_mr"],
};
function migratePerm(perm) {
  const p = Object.assign({}, perm || {});
  for (const [oldK, newKeys] of Object.entries(PERM_MIGRATE)) {
    if (oldK in p) {
      const on = !!p[oldK];
      delete p[oldK];
      for (const k of newKeys) if (!(k in p)) p[k] = on;
    }
  }
  return p;
}

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "Content-Type, X-Token",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
};

const enc = new TextEncoder();
async function sha256hex(s) {
  const h = await crypto.subtle.digest("SHA-256", enc.encode(s));
  return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, "0")).join("");
}
async function hmac(key, msg) {
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", k, enc.encode(msg));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}
const J = (obj, status = 200) => new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", ...CORS } });
const jerr = (msg, status = 400) => J({ ok: false, msg }, status);

// ---------------- token ----------------
async function makeToken(env, u) {
  const exp = Date.now() + 30 * 86400 * 1000;
  const payload = u + "|" + exp;
  const sig = await hmac(env.SECRET || (env.ADMIN_PASS + "|ml"), payload);
  return btoa(unescape(encodeURIComponent(payload + "|" + sig)));
}
async function checkToken(env, token) {
  try {
    const raw = decodeURIComponent(escape(atob(token)));
    const parts = raw.split("|");
    if (parts.length !== 3) return null;
    const [u, exp, sig] = parts;
    if (parseInt(exp) < Date.now()) return null;
    const want = await hmac(env.SECRET || (env.ADMIN_PASS + "|ml"), u + "|" + exp);
    if (sig !== want) return null;
    return u;
  } catch (e) { return null; }
}

// ---------------- KV helpers ----------------
const K_USERS = "users";     // { [u]: {ph, salt, role, note, disabled, perm} }
async function getUsers(env) {
  const raw = await env.KV.get(K_USERS);
  return raw ? JSON.parse(raw) : {};
}
async function putUsers(env, users) {
  await env.KV.put(K_USERS, JSON.stringify(users));
}
async function verifyPw(user, p) {
  const h = await sha256hex(user.salt + p);
  return h === user.ph;
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    const path = url.pathname;
    try { return await route(request, env, path); }
    catch (e) { return jerr("服务异常：" + e.message, 500); }
  }
};

async function route(request, env, path) {
  // ---------- 首次初始化 ----------
  if (path === "/init" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    if (!env.SETUP_CODE || b.code !== env.SETUP_CODE) return jerr("初始化码不对", 403);
    const users = await getUsers(env);
    if (users[env.ADMIN_USER]) return J({ ok: true, msg: "已有管理号，跳过" });
    const salt = crypto.randomUUID().replaceAll("-", "");
    users[env.ADMIN_USER] = {
      ph: await sha256hex(salt + env.ADMIN_PASS), salt, role: "admin",
      note: "管理号", disabled: false, perm: {},
    };
    await putUsers(env, users);
    return J({ ok: true });
  }

  // ---------- 登录 ----------
  if (path === "/login" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const u = String(b.u || "").trim(), p = String(b.p || "");
    if (!u || !p) return jerr("账号和密码都要填");
    const users = await getUsers(env);
    const user = users[u];
    if (!user || user.disabled) return jerr("账号不存在或已停用", 403);
    if (!(await verifyPw(user, p))) return jerr("密码不对", 403);
    const token = await makeToken(env, u);
    return J({ ok: true, token, u, role: user.role || "sub",
               perm: user.role === "admin" ? {} : migratePerm(user.perm) });
  }

  const token = request.headers.get("X-Token") || "";
  const who = await checkToken(env, token);
  if (!who) return jerr("登录已过期，请重新登录", 401);

  // ---------- 启动校验 / 权限拉取 ----------
  if (path === "/perm" && request.method === "GET") {
    const users = await getUsers(env);
    const user = users[who];
    if (!user || user.disabled) return jerr("账号不存在或已停用", 403);
    return J({ ok: true, u: who, role: user.role || "sub",
               perm: user.role === "admin" ? {} : migratePerm(user.perm) });
  }

  if (path === "/changepw" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const users = await getUsers(env);
    const user = users[who];
    if (!user) return jerr("账号不存在", 403);
    if (!(await verifyPw(user, String(b.old || "")))) return jerr("旧密码不对", 403);
    const np = String(b.new || "");
    if (np.length < 4) return jerr("新密码至少 4 位");
    user.ph = await sha256hex(user.salt + np);
    await putUsers(env, users);
    return J({ ok: true });
  }

  // ---------- 管理号：批量保存 ----------
  if (path === "/admin/save" && request.method === "POST") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号能改", 403);
    const b = await request.json().catch(() => ({}));
    const list = Array.isArray(b.users) ? b.users : [];
    for (const it of list) {
      const u = String(it.u || "").trim();
      if (!u || u === who) continue;
      if (!/^[A-Za-z0-9_\-\u4e00-\u9fa5]{2,20}$/.test(u)) return jerr("用户名「" + u + "」不合法（2-20位字母数字中文）");
      const cur = users[u] || {
        salt: crypto.randomUUID().replaceAll("-", ""), role: "sub",
        ph: "", note: "", disabled: false, perm: {},
      };
      if (cur.role === "admin") continue;      // 管理号不可改
      if (it.pw) {                              // 传了密码 = 新建/改密
        if (String(it.pw).length < 4) return jerr(u + " 的密码至少 4 位");
        cur.ph = await sha256hex(cur.salt + String(it.pw));
      }
      if (!cur.ph) return jerr(u + " 还没设密码");
      cur.note = String(it.note || "").slice(0, 50);
      cur.disabled = !!it.disabled;
      const perm = {};
      for (const k of PERM_KEYS) perm[k] = !!(it.perm && it.perm[k]);
      cur.perm = perm;
      users[u] = cur;
    }
    await putUsers(env, users);
    return J({ ok: true });
  }

  if (path === "/admin/list" && request.method === "GET") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号能看", 403);
    const out = Object.entries(users).filter(([u, x]) => x.role !== "admin")
      .map(([u, x]) => ({ u, note: x.note || "", disabled: !!x.disabled,
                          hasPw: !!x.ph, perm: x.perm || {} }));
    return J({ ok: true, keys: PERM_KEYS, users: out });
  }

  // ---------- 发布记录（云端，跟账号走，换电脑登录也在） ----------
  // KV: pub_<u> = { 型号: 标记时间戳ms }
  async function getPub(u) {
    const r = await env.KV.get("pub_" + u);
    return r ? JSON.parse(r) : {};
  }

  // 自己的发布记录
  if (path === "/pub" && request.method === "GET") {
    return J({ ok: true, marks: await getPub(who) });
  }
  // 标记/取消一条：{c: 型号, on: true/false}
  if (path === "/pub" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const c = String(b.c || "").trim();
    if (!c) return jerr("型号不能为空");
    const marks = await getPub(who);
    if (b.on === false) delete marks[c]; else marks[c] = Date.now();
    await env.KV.put("pub_" + who, JSON.stringify(marks));
    return J({ ok: true });
  }
  // 整包合并（登录后把本机旧记录并上云端）：{marks: {c: t}}
  if (path === "/pubsync" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const marks = await getPub(who);
    const inc = b.marks || {};
    for (const [c, t] of Object.entries(inc)) {
      const tt = +t || Date.now();
      if (!(c in marks) || marks[c] < tt) marks[c] = tt;
    }
    await env.KV.put("pub_" + who, JSON.stringify(marks));
    return J({ ok: true, marks });
  }
  // 管理号看所有人（含自己）各发布了什么
  if (path === "/puball" && request.method === "GET") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号能看", 403);
    const out = {};
    for (const u of Object.keys(users)) out[u] = await getPub(u);
    return J({ ok: true, all: out });
  }

  return jerr("接口不存在", 404);
}
