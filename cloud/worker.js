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
  "pubview",     // 「已发布」筛选按钮（看已发布的型号）
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
    // 防误删保护：云端明明有子账号、请求却一个都没带（多半是面板没加载出来）→ 拒绝保存
    const subCount = Object.values(users).filter((x) => x.role !== "admin").length;
    if (subCount > 0 && list.length === 0) return jerr("请求里一个账号都没有，疑似面板没加载好，已拒绝保存");
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
    // 删除：列表里消失的旧子账号 → 移除并清掉 TA 的发布记录/定价/补图残留
    const keep = new Set(list.map((it) => String(it.u || "").trim()));
    for (const u of Object.keys(users)) {
      if (users[u].role === "admin" || u === who) continue;
      if (!keep.has(u)) {
        delete users[u];
        await env.KV.delete("pub_" + u);
        await env.KV.delete("pubp_" + u);
      }
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
  // KV: pub_<u> = { 型号: {t: 标记时间戳ms, k: 'l'|'s' 正品/样机} }（旧客户端值是纯数字，兼容读取）
  //     pubp_<u> = { 型号: 当时的闲鱼定价 }
  const pubTs = (v) => (typeof v === "object" && v ? (+v.t || 0) : (+v || 0));
  async function getPub(u) {
    const r = await env.KV.get("pub_" + u);
    return r ? JSON.parse(r) : {};
  }
  async function getPubP(u) {
    const r = await env.KV.get("pubp_" + u);
    return r ? JSON.parse(r) : {};
  }

  // 自己的发布记录（带各自的闲鱼定价）
  if (path === "/pub" && request.method === "GET") {
    return J({ ok: true, marks: await getPub(who), prices: await getPubP(who) });
  }
  // 标记/取消一条：{c: 型号, on: true/false, k: 'l'|'s', p: 闲鱼定价(可选)}
  if (path === "/pub" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const c = String(b.c || "").trim();
    if (!c) return jerr("型号不能为空");
    const marks = await getPub(who);
    if (b.on === false) {
      delete marks[c];
      const pp = await getPubP(who);
      delete pp[c];
      await env.KV.put("pubp_" + who, JSON.stringify(pp));
    } else {
      marks[c] = { t: Date.now(), k: b.k === "s" ? "s" : "l" };
      if (b.p != null && isFinite(+b.p)) {
        const pp = await getPubP(who);
        pp[c] = +b.p;
        await env.KV.put("pubp_" + who, JSON.stringify(pp));
      }
    }
    await env.KV.put("pub_" + who, JSON.stringify(marks));
    return J({ ok: true });
  }
  // 整包合并（登录后把本机旧记录并上云端）：{marks: {c: {t,k} | t}, prices: {c: 价}}
  if (path === "/pubsync" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const marks = await getPub(who);
    const inc = b.marks || {};
    for (const [c, v] of Object.entries(inc)) {
      const t = pubTs(v) || Date.now();
      const k = typeof v === "object" && v ? (v.k === "s" ? "s" : "l") : "l";
      if (!(c in marks) || pubTs(marks[c]) < t) marks[c] = { t, k };
    }
    await env.KV.put("pub_" + who, JSON.stringify(marks));
    if (b.prices && typeof b.prices === "object") {
      const pp = await getPubP(who);
      for (const [c, p] of Object.entries(b.prices)) {
        if (c in marks && isFinite(+p)) pp[c] = +p;
      }
      await env.KV.put("pubp_" + who, JSON.stringify(pp));
    }
    return J({ ok: true, marks });
  }
  // 管理号看所有**子账号**各发布了什么（带各自的闲鱼定价；管理号自己不在这份名单里）
  if (path === "/puball" && request.method === "GET") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号能看", 403);
    const out = {}, prices = {};
    for (const u of Object.keys(users)) {
      if (users[u].role === "admin") continue;
      out[u] = await getPub(u);
      prices[u] = await getPubP(u);
    }
    return J({ ok: true, all: out, prices });
  }

  // ---------- 补图上云（所有人共享：谁补的大家都能看到） ----------
  // KV: myimgs = { 卡片key: dataURL }；单图压到 220px JPEG（~20KB），总量超 18MB 拒收
  async function getMyImgs(env) {
    const r = await env.KV.get("myimgs");
    return r ? JSON.parse(r) : {};
  }
  if (path === "/myimgs" && request.method === "GET") {
    return J({ ok: true, imgs: await getMyImgs(env) });
  }
  if (path === "/myimg" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const c = String(b.c || "").trim();
    if (!c || !b.b64) return jerr("参数不全");
    if (String(b.b64).length > 400000) return jerr("图太大，压一压再传");
    const imgs = await getMyImgs(env);
    imgs[c] = String(b.b64);
    const total = JSON.stringify(imgs).length;
    if (total > 24000000) return jerr("云端补图库满了（" + Math.round(total/1048576) + "MB），清理后再传");
    await env.KV.put("myimgs", JSON.stringify(imgs));
    return J({ ok: true, total });
  }
  if (path === "/myimg" && request.method === "DELETE") {
    const b = await request.json().catch(() => ({}));
    const c = String(b.c || "").trim();
    if (!c) return jerr("型号不能为空");
    const imgs = await getMyImgs(env);
    delete imgs[c];
    await env.KV.put("myimgs", JSON.stringify(imgs));
    return J({ ok: true });
  }

  // ---------- 用友库存云中转（2026-10-08）：管理号更新 → 全员共享 ----------
  // KV: stocklive = { ts, rows, stock }（约 1.5MB）。管理号 POST 上传，所有登录者 GET 读取。
  // 子账号软件从此不碰用友、不用登用友，库存跟管理号的更新走。
  if (path === "/stockcloud" && request.method === "POST") {
    const users = await getUsers(env);
    if ((users[who] || {}).role !== "admin") return jerr("只有管理号能上传库存", 403);
    const b = await request.json().catch(() => ({}));
    if (!b || typeof b.stock !== "object" || !b.stock || !Object.keys(b.stock).length)
      return jerr("库存数据为空，拒收");
    const obj = { ts: String(b.ts || ""), rows: +b.rows || 0, stock: b.stock,
                  by: who, up: Date.now() };
    await env.KV.put("stocklive", JSON.stringify(obj));
    return J({ ok: true, mb: Math.round(JSON.stringify(obj).length / 1048576 * 10) / 10,
               n: Object.keys(b.stock).length });
  }
  if (path === "/stockcloud" && request.method === "GET") {
    const raw = await env.KV.get("stocklive");
    if (!raw) return jerr("云端还没有共享库存（管理号更新一次后这里就有了）", 404);
    const obj = JSON.parse(raw);
    return J({ ok: true, ts: obj.ts, rows: obj.rows, stock: obj.stock, by: obj.by });
  }
  // 轻量探测：只回时间戳/行数/谁更新的（子账号后台轮询用，不用每次拉 ~0.4MB 全量）
  if (path === "/stockmeta" && request.method === "GET") {
    const raw = await env.KV.get("stocklive");
    if (!raw) return J({ ok: true, ts: "", rows: 0 });
    const obj = JSON.parse(raw);
    return J({ ok: true, ts: obj.ts || "", rows: obj.rows || 0, by: obj.by || "" });
  }

  // ---------- 用友账密（云端存，跟管理号走）----------
  // 2026-10-08 用户口径：不管在哪台电脑登录管理号，都要能自动担起用友更新 →
  // 账密存 KV（yyauth），任何一台登着管理号的机器都能取到并自动登录用友。
  // 仅 admin 可读写；子账号机器走库存云中转，永远不需要这个。
  if (path === "/yyauth" && request.method === "GET") {
    const users = await getUsers(env);
    if ((users[who] || {}).role !== "admin") return jerr("只有管理号能读", 403);
    const raw = await env.KV.get("yyauth");
    const d = raw ? JSON.parse(raw) : null;
    return J({ ok: true, u: (d && d.u) || "", p: (d && d.p) || "" });
  }
  if (path === "/yyauth" && request.method === "POST") {
    const users = await getUsers(env);
    if ((users[who] || {}).role !== "admin") return jerr("只有管理号能设置", 403);
    const b = await request.json().catch(() => ({}));
    if (!b.u || !b.p) return jerr("账号和密码都要填");
    await env.KV.put("yyauth", JSON.stringify({
      u: String(b.u).slice(0, 40), p: String(b.p).slice(0, 60), up: Date.now() }));
    return J({ ok: true });
  }

  // ---------- 软件自更新中转（2026-10-08）：软件内一键升级，客户端不碰 GitHub ----------
  // 私有仓库 → worker 拿 GH_TOKEN 代查/代下。KV 不用；GH_TOKEN 是 secret_text binding。
  const GH_API = "https://api.github.com/repos/BAO-Ww/modelookup/releases/latest";
  async function ghLatest() {
    const r = await fetch(GH_API, { headers: {
      "Authorization": "Bearer " + (env.GH_TOKEN || ""),
      "User-Agent": "modelookup-worker", "Accept": "application/vnd.github+json" } });
    if (!r.ok) return null;
    return r.json();
  }
  function setupAsset(rel) {
    const a = (rel.assets || []).find((x) => /^ModelLookup-Setup-\d[\w.]*\.exe$/i.test(x.name))
           || (rel.assets || []).find((x) => /\.exe$/i.test(x.name));
    return a || null;
  }
  if (path === "/appcheck" && request.method === "GET") {
    const rel = await ghLatest();
    if (!rel || !rel.tag_name) return jerr("GitHub 查询失败（检查 GH_TOKEN 配置）", 502);
    const a = setupAsset(rel);
    return J({ ok: true, tag: rel.tag_name, ver: String(rel.tag_name).replace(/^v/i, ""),
               name: a ? a.name : "", size: a ? a.size : 0 });
  }
  if (path === "/appdl" && request.method === "GET") {
    const rel = await ghLatest();
    if (!rel || !rel.tag_name) return jerr("GitHub 查询失败", 502);
    const a = setupAsset(rel);
    if (!a) return jerr("Release 里没有安装包", 404);
    const dr = await fetch(a.url, { headers: {          // API asset URL + octet-stream = 原始下载
      "Authorization": "Bearer " + (env.GH_TOKEN || ""),
      "User-Agent": "modelookup-worker", "Accept": "application/octet-stream" } });
    if (!dr.ok || !dr.body) return jerr("安装包下载失败（GitHub 侧）", 502);
    const h = { "Content-Type": "application/octet-stream",
                "Content-Disposition": 'attachment; filename="' + a.name + '"' };
    if (dr.headers.get("content-length")) h["Content-Length"] = dr.headers.get("content-length");
    return new Response(dr.body, { headers: h });       // 流式转发，不占 worker 内存
  }

  return jerr("接口不存在", 404);
}
