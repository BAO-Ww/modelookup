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
  "updpanel",    // 更新面板（型号数/数据/库存时间 + 全部更新）—— v2.7.11 漏加导致勾了存不上，v2.7.12 补
  "pubmeta",     // 发布卡片显示「闲鱼定价 / 发布于」小字（2026-10-09：管理号要看，子用户默认不给、要了再勾）
];

// 旧权限键 → 新键映射（v2.5 之前的 perm 只存 guide/clear_price 两个粗键）
const PERM_MIGRATE = {
  guide:       ["p_pt", "p_lp", "p_mp", "p_jd"],
  clear_price: ["p_np", "p_ny", "p_lpz", "p_spz", "p_lb", "p_sb", "p_qty"],
  meidi_cost:  ["p_mh", "p_m2", "p_mr"],
};
function migratePerm(perm) {
  // 2026-10-10 审计修复（静态4）：perm 传字符串/数字会被 Object.assign 拆成下标键，非对象一律归 {}
  const p = Object.assign({}, perm && typeof perm === "object" ? perm : {});
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
// 2026-10-10 审计修复（S3）：SECRET 缺失时不再退回 ADMIN_PASS 签名（那个组合能被猜出 SECRET 的人伪造）
function hmacKey(env) {
  if (!env.SECRET) throw new Error("SECRET 未配置，拒绝签发/校验 token");
  return env.SECRET;
}
// 2026-10-10 审计修复（静态3）：签名比较改恒时，消计时侧信道
function ctEq(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}
// 2026-10-10 审计修复（S4）：对象键名黑名单（__proto__/constructor/prototype 写进对象会原型污染）
const BAD_KEY = /^(__proto__|constructor|prototype)$/;
function safeKey(s, max) {
  s = String(s == null ? "" : s).trim().slice(0, max || 100);
  return BAD_KEY.test(s) ? "" : s;
}
// 2026-10-10 审计修复（G6-G14）：KV 读 JSON 统一兜底——损坏当 fallback + console.error，不再 500 卡死功能
async function getJSON(env, key, fallback) {
  try {
    const raw = await env.KV.get(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch (e) {
    console.error("KV 键 " + key + " JSON 损坏：" + e.message);
    return fallback;
  }
}
// 2026-10-10 审计修复（G15/并发）：进程内 async 锁——同 isolate 的读改写操作串行化，消并发丢更新。
// Cloudflare Worker 同一 isolate 串行处理请求，但 fetch 是异步交错执行的，全路由套一把锁最稳。
let _lock = Promise.resolve();
function withLock(fn) {
  const run = _lock.then(fn, fn);
  _lock = run.then(function () {}, function () {});
  return run;
}
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
// 2026-10-10 审计修复（静态2）：token 里烤进密码版本号 pv——改密码后旧 token（含被偷的）全部作废。
// 旧格式 3 段（u|exp|sig，无 pv）兼容读取：视为 pv="0"，等价于用户从未改过密。
async function makeToken(env, u, pv) {
  const exp = Date.now() + 30 * 86400 * 1000;
  const payload = u + "|" + exp + "|" + String(pv || 0);
  const sig = await hmac(hmacKey(env), payload);
  return btoa(unescape(encodeURIComponent(payload + "|" + sig)));
}
async function checkToken(env, token, users) {
  try {
    const raw = decodeURIComponent(escape(atob(token)));
    const parts = raw.split("|");
    if (parts.length !== 3 && parts.length !== 4) return null;
    const u = parts[0], exp = parts[1];
    const pv = parts.length === 4 ? parts[2] : "0";
    const sig = parts[parts.length - 1];
    if (parseInt(exp) < Date.now()) return null;
    const want = await hmac(hmacKey(env),
      u + "|" + exp + (parts.length === 4 ? "|" + pv : ""));
    if (!ctEq(sig, want)) return null;
    const user = users ? users[u] : null;
    if (user && String(user.pv || 0) !== pv) return null;   // 改密后旧 token 作废
    return u;
  } catch (e) { return null; }
}

// ---------------- KV helpers ----------------
const K_USERS = "users";     // { [u]: {ph, salt, role, note, disabled, perm, pv} }
async function getUsers(env) {
  return getJSON(env, K_USERS, {});
}
async function putUsers(env, users) {
  await env.KV.put(K_USERS, JSON.stringify(users));
}
async function verifyPw(user, p) {
  const h = await sha256hex(user.salt + p);
  return h === user.ph;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    const path = url.pathname;
    try {
      // 全路由串行化（审计 G15）：读改写型操作（pub/pubsync/admin/save/myimgs…）不再有并发丢更新
      return await withLock(() => route(request, env, path, ctx));
    }
    catch (e) { return jerr("服务异常：" + e.message, 500); }
  }
};

async function route(request, env, path, ctx) {
  // 2026-10-12 修复：/appcheck /appdl 用 url.searchParams 取 ?plat=，但 url 从未在
  // route() 作用域定义（它定义在外层 fetch 里）→ 检查更新必抛「url is not defined」
  const url = new URL(request.url);
  // ---------- 首次初始化 ----------
  if (path === "/init" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    if (!env.SETUP_CODE || b.code !== env.SETUP_CODE) return jerr("初始化码不对", 403);
    // 审计（静态8）：init 成功后记 inited，之后拒绝再次 init——就算 users KV 被清空，
    // 持有 SETUP_CODE 的人也不能用 env.ADMIN_PASS 重建设同样密码的管理号。
    // 万一真要重置：到 CF 控制台把 KV 键 inited 删掉再 init。
    if ((await env.KV.get("inited")) === "1") return jerr("已初始化过，如确需重置请到 CF 控制台删 KV 键 inited", 403);
    const users = await getUsers(env);
    if (users[env.ADMIN_USER]) {
      await env.KV.put("inited", "1");
      return J({ ok: true, msg: "已有管理号，跳过" });
    }
    const salt = crypto.randomUUID().replaceAll("-", "");
    users[env.ADMIN_USER] = {
      ph: await sha256hex(salt + env.ADMIN_PASS), salt, role: "admin",
      note: "管理号", disabled: false, perm: {}, pv: 0,
    };
    await putUsers(env, users);
    await env.KV.put("inited", "1");
    return J({ ok: true });
  }

  // ---------- 登录 ----------
  if (path === "/login" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const u = String(b.u || "").trim(), p = String(b.p || "");
    if (!u || !p) return jerr("账号和密码都要填");
    // 审计（静态1）防爆破：按「账号+天」计失败次数，一天错 10 次锁 1 小时（KV 尽力而为）
    const day = new Date().toISOString().slice(0, 10);
    const fk = "faillog_" + u;
    const fl = await getJSON(env, fk, { d: "", n: 0, lock: 0 });
    if (fl.d === day && fl.n >= 10) {
      if (Date.now() < (fl.lock || 0))
        return jerr("失败次数过多，请 " + Math.ceil((fl.lock - Date.now()) / 60000) + " 分钟后再试", 429);
      fl.n = 0; fl.lock = 0;                     // 锁期满，重新计数
    }
    const users = await getUsers(env);
    const user = users[u];
    if (!user || user.disabled) return jerr("账号不存在或已停用", 403);
    if (!(await verifyPw(user, p))) {
      if (fl.d !== day) { fl.d = day; fl.n = 0; }
      fl.n++;
      if (fl.n >= 10) fl.lock = Date.now() + 3600000;
      try { await env.KV.put(fk, JSON.stringify(fl)); } catch (e) {}
      return jerr("密码不对", 403);
    }
    try { await env.KV.delete(fk); } catch (e) {}   // 登录成功清失败计数
    const token = await makeToken(env, u, user.pv);
    return J({ ok: true, token, u, role: user.role || "sub",
               perm: user.role === "admin" ? {} : migratePerm(user.perm) });
  }

  const token = request.headers.get("X-Token") || "";
  const usersGate = await getUsers(env);       // 审计（G2/G3）：所有带 token 接口统一校验账号还在、没停用
  const who = await checkToken(env, token, usersGate);
  if (!who) return jerr("登录已过期，请重新登录", 401);
  if (!usersGate[who]) return jerr("账号不存在或已删除，请重新登录", 403);
  if (usersGate[who].disabled) return jerr("账号已停用", 403);

  // 在线心跳已彻底下线（2026-10-10 深夜，用户最终拍板不要了）：
  // KV 免费额度 1000 写/天，心跳哪怕 60 分钟节流也是纯开销，砍掉写入口零成本。

  // ---------- 启动校验 / 权限拉取 ----------
  if (path === "/perm" && request.method === "GET") {
    const users = await getUsers(env);
    const user = users[who];
    if (!user || user.disabled) return jerr("账号不存在或已停用", 403);
    return J({ ok: true, u: who, role: user.role || "sub",
               perm: user.role === "admin" ? {} : migratePerm(user.perm) });
  }

  // 管理号「我的显示开关」上云（2026-10-09）：跟账号走，换电脑登录不用重弄
  // KV: selfhide_<u> = { 权限key: true(=隐藏) }；子账号无此设置（显示由管理号 perm 管）
  if (path === "/selfhide" && request.method === "GET") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号有显示开关", 403);
    const r = await env.KV.get("selfhide_" + who);
    let hide = {};
    try { hide = r ? JSON.parse(r) : {}; } catch (e) {}
    return J({ ok: true, hide: hide && typeof hide === "object" ? hide : {} });
  }
  if (path === "/selfhide" && request.method === "POST") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号有显示开关", 403);
    const b = await request.json().catch(() => ({}));
    if (!b.hide || typeof b.hide !== "object") return jerr("参数错误");
    await env.KV.put("selfhide_" + who, JSON.stringify(b.hide));
    return J({ ok: true });
  }

  // /presence 接口已随心跳一起下线（2026-10-10 深夜）：
  // 没有写入口了，读出来也全是死数据。旧客户端模板对 404 有 .catch + 空值兜底，不会崩。

  if (path === "/changepw" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const users = await getUsers(env);
    const user = users[who];
    if (!user) return jerr("账号不存在", 403);
    if (!(await verifyPw(user, String(b.old || "")))) return jerr("旧密码不对", 403);
    const np = String(b.new || "");
    if (np.length < 4) return jerr("新密码至少 4 位");
    user.ph = await sha256hex(user.salt + np);
    user.pv = (user.pv || 0) + 1;      // 审计（静态2）：密码版本号 +1，旧 token 全部作废，客户端自动重新登录
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
      // 审计（S4）：__proto__/constructor/prototype 当用户名会原型污染整个 users 对象
      if (BAD_KEY.test(u)) return jerr("用户名「" + u + "」不合法");
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
    // 审计（S5）防误删补齐：原来只拦「list 全空」，list=[自己] 这类「面板少渲染」场景会把
    // 全部子账号连 pub_/pubp_ 删光。现在只要删名单非空就要求 confirm:true 明确确认。
    const keep = new Set(list.map((it) => String(it.u || "").trim()));
    const doomed = Object.keys(users).filter((u) => users[u].role !== "admin" && u !== who && !keep.has(u));
    if (doomed.length && b.confirm !== true)
      return jerr("本次保存将删除子账号：" + doomed.join(", ") + "。如果是误操作请检查面板；确认要删请带 confirm:true", 400);
    for (const u of doomed) {
      delete users[u];
      await env.KV.delete("pub_" + u);
      await env.KV.delete("pubp_" + u);
    }
    await putUsers(env, users);
    return J({ ok: true, deleted: doomed });
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
    return getJSON(env, "pub_" + u, {});
  }
  async function getPubP(u) {
    return getJSON(env, "pubp_" + u, {});
  }

  // 自己的发布记录（带各自的闲鱼定价）
  if (path === "/pub" && request.method === "GET") {
    return J({ ok: true, marks: await getPub(who), prices: await getPubP(who) });
  }
  // 标记/取消一条：{c: 型号, on: true/false, k: 'l'|'s', p: 闲鱼定价(可选)}
  if (path === "/pub" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const c = safeKey(b.c, 100);                       // 审计（G1/S4）：长度上限 + 键名黑名单
    if (!c) return jerr("型号不能为空");
    const marks = await getPub(who);
    if (b.on === false) {
      delete marks[c];
      // 2026-10-10 新规矩：取消发布，定价一并清掉（旧的「价格跟型号走」作废）
      const pp0 = await getPubP(who);
      if (c in pp0) { delete pp0[c]; await env.KV.put("pubp_" + who, JSON.stringify(pp0)); }
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
    if (Array.isArray(b.prices)) return jerr("prices 格式错误（应为对象）");   // 审计：数组下标会被当型号
    const marks = await getPub(who);
    const inc = b.marks && typeof b.marks === "object" && !Array.isArray(b.marks) ? b.marks : {};
    for (const [c0, v] of Object.entries(inc)) {
      const c = safeKey(c0, 100);
      if (!c) continue;                                // 审计（S4）：脏键直接丢
      const t = pubTs(v) || Date.now();
      const k = typeof v === "object" && v ? (v.k === "s" ? "s" : "l") : "l";
      if (!(c in marks) || pubTs(marks[c]) < t) marks[c] = { t, k };
    }
    // KV 省额度（2026-10-10）：只有真的并进来了发布标记才写 pub_——
    // 打价场景每次 /pubsync 只带 prices，pub_ 原来被无条件整包重写，白烧一倍写额度
    if (Object.keys(inc).length) await env.KV.put("pub_" + who, JSON.stringify(marks));
    if (b.prices && typeof b.prices === "object" && !Array.isArray(b.prices)) {
      const pp = await getPubP(who);
      for (const [c0, p] of Object.entries(b.prices)) {
        const c = safeKey(c0, 100);
        if (!c) continue;
        // 2026-10-08：全量收价 —— 没点「标记发布」的型号定价也要存（用户要求）
        if (isFinite(+p)) pp[c] = +p;
      }
      await env.KV.put("pubp_" + who, JSON.stringify(pp));
    }
    return J({ ok: true, marks, prices: await getPubP(who) });
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

  // 定点清理定价表（2026-10-10）：管理号删掉指定账号 pubp_ 里的指定键。
  // /pubsync 的合并只增不删——泄漏/串号写进去的脏价永远灌得回来，必须有个能删的口子。
  if (path === "/pubclean" && request.method === "POST") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号能清理", 403);
    const b = await request.json().catch(() => ({}));
    const u = String(b.u || who);
    if (!users[u]) return jerr("没有这个账号", 404);
    const keys = Array.isArray(b.keys) ? b.keys.map((k) => safeKey(k, 100)).filter(Boolean) : [];
    const pp = await getPubP(u);
    const removed = [];
    for (const k of keys) { if (k in pp) { delete pp[k]; removed.push(k); } }
    await env.KV.put("pubp_" + u, JSON.stringify(pp));
    return J({ ok: true, u, removed, prices: pp });
  }

  // ---------- 补图上云（所有人共享：谁补的大家都能看到） ----------
  // KV: myimgs = { 卡片key: dataURL }；单图压到 220px JPEG（~20KB），总量超 18MB 拒收
  async function getMyImgs(env) {
    return getJSON(env, "myimgs", {});
  }
  if (path === "/myimgs" && request.method === "GET") {
    return J({ ok: true, imgs: await getMyImgs(env) });
  }
  if (path === "/myimg" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const c = safeKey(b.c, 100);                       // 审计（S4）：键名黑名单
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
    const c = safeKey(b.c, 100);
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
    // sbc = 按物料编码聚合的库存桶（2026-10-10「编码优先」）：编码是 SKU 唯一码，
    // 前端有编码的卡片走它做一对一硬核对。可选字段，旧客户端不带也能传。
    const sbc = (b.sbc && typeof b.sbc === "object" && !Array.isArray(b.sbc)) ? b.sbc : null;
    const obj = { ts: String(b.ts || "").slice(0, 64), rows: +b.rows || 0, stock: b.stock,
                  sbc: sbc || undefined, by: who, up: Date.now() };
    const payload = JSON.stringify(obj);
    if (payload.length > 3000000) return jerr("库存数据过大（" + (payload.length / 1048576).toFixed(1) + "MB），拒收", 413);
    await env.KV.put("stocklive", payload);
    return J({ ok: true, mb: Math.round(payload.length / 1048576 * 10) / 10,
               n: Object.keys(b.stock).length });
  }
  if (path === "/stockcloud" && request.method === "GET") {
    const obj = await getJSON(env, "stocklive", null);
    if (!obj) return jerr("云端还没有共享库存（管理号更新一次后这里就有了）", 404);
    return J({ ok: true, ts: obj.ts, rows: obj.rows, stock: obj.stock, sbc: obj.sbc || null, by: obj.by });
  }
  // 轻量探测：只回时间戳/行数/谁更新的（子账号后台轮询用，不用每次拉 ~0.4MB 全量）
  // 数据过期自动触发（2026-10-10）：软件开着时每 60~90 秒探一次 meta，
  // 发现数据超过 30 分钟没更新就自动让 GitHub 拉一轮（30 分钟节流，凌晨 0-8 点不跑）。
  // 没人开软件就永远不会触发——定时 cron 每小时一次照旧兜底。
  async function autoKick(kind, ageMs) {
    // 检查+登记（在 withLock 串行区里跑，两个客户端同时探测也只会登记一次）。
    // 返回 true = 本轮由我触发，调用方拿 waitUntil 去真正 dispatch。
    // 阈值 90 分钟（2026-10-10 深夜，KV 省额度整改）：cron 每小时一跑，
    // 阈值 30 分钟时每轮都抢在 cron 前面白触发一遍（ci_auto put + 多余 CI 跑 ≈ 64 写/天）。
    // 改成 90 分钟 = 只在 cron 真的断更（漏 1-2 轮）时才兜底触发，平时零写入。
    try {
      const bjHour = new Date(Date.now() + 8 * 3600 * 1000).getUTCHours();
      if (bjHour < 8) return false;                  // 0-8 点不拉（用户口径）
      if (ageMs < 90 * 60000) return false;          // cron 每小时刷新，90 分钟内都是新鲜的
      const key = "ci_auto_" + kind;
      const la = +(await env.KV.get(key)) || 0;
      if (Date.now() - la < 30 * 60000) return false; // 刚自动触发过，节流
      await env.KV.put(key, String(Date.now()));
      return true;
    } catch (e) { return false; }
  }
  if (path === "/stockmeta" && request.method === "GET") {
    const obj = await getJSON(env, "stocklive", null);
    if (!obj) return J({ ok: true, ts: "", rows: 0 });
    if (ctx && ctx.waitUntil && await autoKick("yy", Date.now() - (+obj.up || 0)))
      ctx.waitUntil(ghDispatch("yonyou-cloud-stock.yml"));
    return J({ ok: true, ts: obj.ts || "", rows: obj.rows || 0, by: obj.by || "" });
  }

  // ---------- 金山表格云端中转（2026-10-09 跟库存同款）----------
  // GitHub Actions 定时（云端浏览器）匿名拉「公布版 + 价格表」→ 这里中转；
  // 每台机器的软件每 60 秒轻探 /xlsxmeta，内容有变（ts=两个文件内容的哈希）就拉全量重建。
  // ts 是内容哈希：表格没改就不会变，机器不会被无谓打扰。
  if (path === "/xlsxcloud" && request.method === "POST") {
    const users = await getUsers(env);
    // 审计（优化2）：X-Cloud-Key 建议配独立 CLOUD_KEY；没配时兼容沿用 SECRET（CI 在用，不弄断）
    const ck = env.CLOUD_KEY || env.SECRET;
    const isCloud = !!ck && (request.headers.get("X-Cloud-Key") || "") === ck;
    if ((users[who] || {}).role !== "admin" && !isCloud) return jerr("没有上传权限", 403);
    const b = await request.json().catch(() => ({}));
    if (!b || !b.ts || !b.files || !b.files["data.xlsx"]) return jerr("表格数据为空，拒收");
    const obj = { ts: String(b.ts).slice(0, 64), files: b.files, by: isCloud ? "云端定时" : who, up: Date.now() };
    await env.KV.put("xlsxlive", JSON.stringify(obj));
    return J({ ok: true, mb: Math.round(JSON.stringify(obj).length / 1048576 * 10) / 10 });
  }
  if (path === "/xlsxcloud" && request.method === "GET") {
    const obj = await getJSON(env, "xlsxlive", null);
    if (!obj) return jerr("云端还没有共享表格（等云端定时任务跑一次）", 404);
    return J({ ok: true, ts: obj.ts, files: obj.files, by: obj.by });
  }
  if (path === "/xlsxmeta" && request.method === "GET") {
    const obj = await getJSON(env, "xlsxlive", null);
    if (!obj) return J({ ok: true, ts: "" });
    if (ctx && ctx.waitUntil && await autoKick("kd", Date.now() - (+obj.up || 0)))
      ctx.waitUntil(ghDispatch("kdocs-cloud-data.yml"));
    return J({ ok: true, ts: obj.ts || "", by: obj.by || "" });
  }

  // ---------- 金山登录态中转（2026-10-09）----------
  // 云端浏览器没有金山会话，匿名拉表会被拒（实测 userNotLogin）。
  // 管理号机器把自己 WebView2 里的 kdocs cookies 提取出来存这里（CDP 抓取），
  // 云端定时任务每次跑之前取走注入浏览器。仅管理号可读写。
  if (path === "/kdauth" && request.method === "POST") {
    const users = await getUsers(env);
    if ((users[who] || {}).role !== "admin") return jerr("只有管理号能存金山登录态", 403);
    const b = await request.json().catch(() => ({}));
    if (!b || !Array.isArray(b.cookies) || !b.cookies.length) return jerr("cookies 为空，拒收");
    await env.KV.put("kdauth", JSON.stringify({ cookies: b.cookies, by: who, up: Date.now() }));
    return J({ ok: true, n: b.cookies.length });
  }
  if (path === "/kdauth" && request.method === "GET") {
    // 审计（S1）严重越权修复：GET 原来只验 token 不验角色，子账号能拿到金山完整 cookies。
    // 云端定时任务（GitHub Actions）也走这个口——它用管理号 token，401 时退 X-Cloud-Key，
    // 所以两条通道都要放行，否则云端拉表会断。
    const users = await getUsers(env);
    const ck = env.CLOUD_KEY || env.SECRET;
    const isCloud = !!ck && (request.headers.get("X-Cloud-Key") || "") === ck;
    if ((users[who] || {}).role !== "admin" && !isCloud) return jerr("只有管理号能读金山登录态", 403);
    const obj = await getJSON(env, "kdauth", null);
    if (!obj) return jerr("云端还没有金山登录态（管理号机器上传一次）", 404);
    return J({ ok: true, cookies: obj.cookies || [], up: obj.up || 0 });
  }

  // ---------- 用友账密（云端存，跟管理号走）----------
  // 2026-10-08 用户口径：不管在哪台电脑登录管理号，都要能自动担起用友更新 →
  // 账密存 KV（yyauth），任何一台登着管理号的机器都能取到并自动登录用友。
  // 仅 admin 可读写；子账号机器走库存云中转，永远不需要这个。
  if (path === "/yyauth" && request.method === "GET") {
    const users = await getUsers(env);
    if ((users[who] || {}).role !== "admin") return jerr("只有管理号能读", 403);
    const d = await getJSON(env, "yyauth", null);
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

  // ---------- 用友登录失败告警（2026-10-08）----------
  // 自动登录失败（密码不对/冒验证码）→ 任意登录者 POST 上报；管理号软件每分钟轮询
  // GET，有新告警就在页面顶部亮横幅。KV: yyalert = { ts, msg, by }
  if (path === "/yyalert" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const msg = String(b.msg || "").trim().slice(0, 200);
    if (!msg) return jerr("告警内容为空");
    const obj = { ts: Date.now(), msg, by: who };
    await env.KV.put("yyalert", JSON.stringify(obj));
    return J({ ok: true });
  }
  if (path === "/yyalert" && request.method === "GET") {
    const d = await getJSON(env, "yyalert", null);
    return J({ ok: true, ts: (d && d.ts) || 0, msg: (d && d.msg) || "", by: (d && d.by) || "" });
  }

  // ---------- 软件自更新中转（2026-10-08）：软件内一键升级，客户端不碰 GitHub ----------
  // 私有仓库 → worker 拿 GH_TOKEN 代查/代下。KV 不用；GH_TOKEN 是 secret_text binding。
  const GH_API = "https://api.github.com/repos/BAO-Ww/modelookup/releases/latest";
  // 审计（G4/优化）：GH 429 单独提示；200 但坏 JSON 不再裸抛 500
  async function ghLatest() {
    let r;
    try {
      r = await fetch(GH_API, { headers: {
        "Authorization": "Bearer " + (env.GH_TOKEN || ""),
        "User-Agent": "modelookup-worker", "Accept": "application/vnd.github+json" } });
    } catch (e) { return { __fail: "GitHub 网络不通（" + e.message + "）" }; }
    if (r.status === 429) return { __fail: "GitHub 限流中，稍后再试" };
    if (!r.ok) return { __fail: "GitHub 查询失败 HTTP " + r.status + "（检查 GH_TOKEN 配置）" };
    try { return await r.json(); } catch (e) { return { __fail: "GitHub 返回了异常数据（坏 JSON）" }; }
  }
  function setupAsset(rel, plat) {
    // 2026-10-11 Mac 端：?plat=mac 匹配 ModelLookup-mac-*.zip；不带 plat 仍走 exe（老客户端零影响）
    if (plat === "mac") {
      return (rel.assets || []).find((x) => /^ModelLookup-mac-[\w.]*\.zip$/i.test(x.name))
          || (rel.assets || []).find((x) => /^ModelLookup-mac.*\.zip$/i.test(x.name)) || null;
    }
    const a = (rel.assets || []).find((x) => /^ModelLookup-Setup-\d[\w.]*\.exe$/i.test(x.name))
           || (rel.assets || []).find((x) => /\.exe$/i.test(x.name));
    return a || null;
  }
  if (path === "/appcheck" && request.method === "GET") {
    const rel = await ghLatest();
    if (rel.__fail) return jerr(rel.__fail, 502);
    if (!rel.tag_name) return jerr("GitHub 上没有 Release（检查 GH_TOKEN 配置）", 502);
    const a = setupAsset(rel, url.searchParams.get("plat") || "");
    return J({ ok: true, tag: rel.tag_name, ver: String(rel.tag_name).replace(/^v/i, ""),
               name: a ? a.name : "", size: a ? a.size : 0 });
  }
  if (path === "/appdl" && request.method === "GET") {
    const rel = await ghLatest();
    if (rel.__fail) return jerr(rel.__fail, 502);
    if (!rel.tag_name) return jerr("GitHub 查询失败", 502);
    const a = setupAsset(rel, url.searchParams.get("plat") || "");
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

  // ---------- 云端更新中转（2026-10-09 v2.8 全云端口径）----------
  // 「全部更新」= 软件让 worker 拿 GH_TOKEN 触发 GitHub Actions（金山拉表 / 用友拉库存），
  // 跑完各机器软件从 /xlsxcloud /stockcloud 自动同步。GH_TOKEN 需带 workflow 权限。
  const CI_WF = { yy: "yonyou-cloud-stock.yml", kd: "kdocs-cloud-data.yml" };
  async function ghDispatch(wf) {
    const r = await fetch(
      "https://api.github.com/repos/BAO-Ww/modelookup/actions/workflows/" + wf + "/dispatches",
      { method: "POST",
        headers: { "Authorization": "Bearer " + (env.GH_TOKEN || ""),
                   "User-Agent": "modelookup-worker", "Accept": "application/vnd.github+json",
                   "Content-Type": "application/json" },
        body: JSON.stringify({ ref: "main" }) });
    return r.status === 204;
  }
  async function ghLastRun(wf) {
    const r = await fetch(
      "https://api.github.com/repos/BAO-Ww/modelookup/actions/workflows/" + wf + "/runs?per_page=1",
      { headers: { "Authorization": "Bearer " + (env.GH_TOKEN || ""),
                   "User-Agent": "modelookup-worker", "Accept": "application/vnd.github+json" } });
    if (!r.ok) return null;
    const d = await r.json();
    const run = (d.workflow_runs || [])[0];
    return run ? { id: run.id, num: run.run_number, status: run.status,
                   con: run.conclusion, at: run.created_at } : null;
  }
  if (path === "/citrigger" && request.method === "POST") {
    // 审计（S2）：原来任何登录者都能触发 GitHub Actions。现在限管理号，
    // 或被明确勾了「更新面板」权限的子账号（面板上有更新按钮的人）；外加 60 秒节流。
    const users = await getUsers(env);
    const me = users[who] || {};
    if (me.role !== "admin" && !(me.perm && me.perm.updpanel))
      return jerr("没有触发云端更新的权限（要管理号，或勾了更新面板权限）", 403);
    const lt = +(await env.KV.get("ci_last")) || 0;
    if (Date.now() - lt < 60000)
      return jerr("刚触发过，1 分钟内不用重复点（云端任务跑完各机器会自动同步）", 429);
    const b = await request.json().catch(() => ({}));
    const want = String((b && b.wf) || "all");            // all | yy | kd
    if (want !== "all" && want !== "yy" && want !== "kd") return jerr("wf 参数不合法（all / yy / kd）", 400);
    const prev = {};
    for (const k of Object.keys(CI_WF)) prev[k] = (await ghLastRun(CI_WF[k]) || {}).id || 0;
    const out = {};
    if (want === "all" || want === "yy") out.yy = await ghDispatch(CI_WF.yy);
    if (want === "all" || want === "kd") out.kd = await ghDispatch(CI_WF.kd);
    if (!Object.keys(out).length || !Object.values(out).every(Boolean))
      return jerr("触发云端任务失败（检查 GH_TOKEN 是否有 workflow 权限）", 502);
    // 节流时间戳尽力而为：KV 额度爆的当天写不进去也别把整个触发搞失败（2026-10-10）
    try { await env.KV.put("ci_last", String(Date.now())); } catch (e) {}   // 触发成功才计时，失败可立即重试
    return J({ ok: true, triggered: out, at: new Date().toISOString(), prev });
  }
  if (path === "/cistatus" && request.method === "GET") {
    const yy = await ghLastRun(CI_WF.yy);
    const kd = await ghLastRun(CI_WF.kd);
    if (!yy || !kd) return jerr("GitHub 查询失败（检查 GH_TOKEN）", 502);
    return J({ ok: true, yy, kd });
  }

  return jerr("接口不存在", 404);
}
