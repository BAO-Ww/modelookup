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
  "copy",        // 文案生成（闲鱼/小红书）—— 2026-10-13 漏加导致勾了存不上（同 v2.7.11 updpanel 坑）
  "pubview",     // 「已发布」筛选按钮（看已发布的型号）
  "updpanel",    // 更新面板（型号数/数据/库存时间 + 全部更新）—— v2.7.11 漏加导致勾了存不上，v2.7.12 补
  "pubmeta",     // 发布卡片显示「闲鱼定价 / 发布于」小字（2026-10-09：管理号要看，子用户默认不给、要了再勾）
  "xysj",        // 闲鱼数据（数据管家）入口——主区内嵌显示（2026-10-17；后台入口同日删，键下架）
  "xyops",       // 闲鱼操作链接（编辑/改价/查看/下架）显示开关（2026-10-17）
  "xysoft",      // 闲鱼操作打开方式：勾=软件内嵌（默认），不勾=系统浏览器（2026-10-17）
  "tbyp",        // 婷宝电器优选（出图出文案）入口——内嵌婷宝版文案窗（2026-10-17，独立权限管理号定给谁）
  "kitset",      // 蓝底套图入口（2026-10-23 独立权限开关，原来搭「写文案 copy」的车没法单独关）
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
// ---------------- D1：pub_/pubp_ 存取层（2026-10-15 迁移） ----------------
// KV 免费版写 1000/天被批量操作打爆（10-14 迁移 32 并发丢 16 条实锤），且 KV 无原子读改写。
// pub_/pubp_（发布记录/价格镜像，高频写）迁 D1：免费 10 万行写/天（KV 的 100 倍），
// 单行 UPSERT 强一致，同 isolate 还有 withLock 串行 —— 并发覆盖从根上治掉。
// users/xy_/myimgs/snap_ 留 KV（低频写 + 登录态不动）。
// 表结构：store(k TEXT PRIMARY KEY, v TEXT, updated_at INTEGER)，k 与 KV 键名同构（pub_B/pubp_B…）。
// env.DB 未绑（部署漏 binding）→ 退回 KV 不炸；D1 已绑但读写失败 → 抛错，绝不退 KV 读旧数据。
let _d1ok = null;   // null=没试过，true/false=建表结果（每 isolate 一次）
async function d1Init(env) {
  if (_d1ok !== null) return _d1ok;
  _d1ok = false;
  if (!env.DB) return false;
  await env.DB.prepare("CREATE TABLE IF NOT EXISTS store (k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at INTEGER)").run();
  _d1ok = true;
  return true;
}
async function d1Get(env, key, fallback) {
  if (!(await d1Init(env))) return getJSON(env, key, fallback);
  const r = await env.DB.prepare("SELECT v FROM store WHERE k = ?").bind(key).first();
  return r && r.v != null ? JSON.parse(r.v) : fallback;
}
async function d1Put(env, key, obj) {
  const body = JSON.stringify(obj);
  if (!(await d1Init(env))) { await env.KV.put(key, body); return; }
  await env.DB.prepare(
    "INSERT INTO store (k,v,updated_at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at"
  ).bind(key, body, Date.now()).run();
}
async function d1Del(env, key) {
  if (!(await d1Init(env))) { await env.KV.delete(key); return; }
  await env.DB.prepare("DELETE FROM store WHERE k = ?").bind(key).run();
}
// 快照用：D1 全表平铺（键就 pub_/pubp_ 每账号一个，量小）；失败返回空数组让快照降级
async function d1List(env) {
  if (!(await d1Init(env))) return [];
  const r = await env.DB.prepare("SELECT k, v FROM store").all();
  return (r.results || []).map((x) => ({ name: x.k, v: x.v }));
}

// ---------------- myimgs 补图行级存储（2026-10-24 D1 化）----------------
// 之前 myimgs = KV 一坨整包（1399 张 / 8.9MB），每次传/删一张 = 读 8.9MB 改一张写 8.9MB——
// 并发覆盖 + 写放大 + 逼近 KV 单值 25MB 上限。现在一图一行 myimg_<卡片key>（值 {b,by}）：
//   · 传图/删图 = 单行 UPSERT/DELETE，原子且并发安全
//   · 读全量分页（D1 单查询响应有上限，1399 行约 8.9MB 一口吞不下）
//   · KV 键 "myimgs" 保留作惰性迁移源（D1 首次读到空时整包搬入），迁移后可手动清掉
//   · env.DB 未绑 → 退 KV 整包（老部署兼容）
const MI_PFX = "myimg_";
async function myimgsD1All(env) {
  const out = {};
  const PAGE = 120;
  for (let off = 0; ; off += PAGE) {
    const r = await env.DB.prepare(
      "SELECT k, v FROM store WHERE substr(k,1,6) = ? ORDER BY k LIMIT ? OFFSET ?"
    ).bind(MI_PFX, PAGE, off).all();
    const rows = r.results || [];
    for (const x of rows) {
      try { out[x.k.slice(MI_PFX.length)] = JSON.parse(x.v); } catch (e) {}
    }
    if (rows.length < PAGE) break;
  }
  return out;
}
async function myimgsGet(env) {
  if (!(await d1Init(env))) return getJSON(env, "myimgs", {});
  const n = await env.DB.prepare("SELECT count(*) AS n FROM store WHERE substr(k,1,6) = ?").bind(MI_PFX).first();
  if (!n || !n.n) {
    const raw = await env.KV.get("myimgs");          // 惰性迁移（只发生一次；之后 KV 键不再被读）
    if (raw) {
      try {
        const obj = JSON.parse(raw);
        if (obj && typeof obj === "object") {
          const ks = Object.keys(obj);
          for (let i = 0; i < ks.length; i += 40) {
            const st = [];
            for (const k of ks.slice(i, i + 40))
              st.push(env.DB.prepare(
                "INSERT INTO store (k,v,updated_at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at"
              ).bind(MI_PFX + k, JSON.stringify(obj[k]), Date.now()));
            await env.DB.batch(st);
          }
          return obj;
        }
      } catch (e) {}
    }
    return {};
  }
  return myimgsD1All(env);
}
async function myimgsPut(env, imgs) {               // 整包形态 = 与 D1 现状 diff（行级 upsert/delete）
  if (!(await d1Init(env))) { await env.KV.put("myimgs", JSON.stringify(imgs)); return; }
  const cur = await myimgsD1All(env);
  const st = [];
  for (const [k, v] of Object.entries(imgs)) {
    const s = JSON.stringify(v);
    if (cur[k] === undefined || JSON.stringify(cur[k]) !== s)
      st.push(env.DB.prepare(
        "INSERT INTO store (k,v,updated_at) VALUES (?,?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at"
      ).bind(MI_PFX + k, s, Date.now()));
  }
  for (const k of Object.keys(cur))
    if (imgs[k] === undefined)
      st.push(env.DB.prepare("DELETE FROM store WHERE k = ?").bind(MI_PFX + k));
  for (let i = 0; i < st.length; i += 40) await env.DB.batch(st.slice(i, i + 40));
}

// ---------------- 标记发布行级存储（2026-10-15 大改）----------------
// 之前 pub_<u> = 整个账号一坨 JSON，每次标记 = 读整坨改一条写整坨——并发覆盖/写放大/单点损坏
// 三宗罪的根源。现在拆三张表，一条标记一行，写入原子化：
//   marks(u,c,xy,k,t,p,xid)   一条发布记录一行（xy = 闲鱼号|口径组合键，与 blob xys 键同构）
//   markmeta(u,c,k,t,cs_l,cs_v,bn_lb,bn_sb,nat)  型号级镜像（顶层 t/k + cs/bn/nat）
//   prices(u,c,p)             价格镜像（原 pubp_，含没发布过只收过价的历史型号）
// 旧 pub_*/pubp_* blob 迁移后原样留在 store 表当只读备份，worker 不再读写它们。
const MARKS_TABLES_SQL = [
  "CREATE TABLE IF NOT EXISTS marks (u TEXT NOT NULL, c TEXT NOT NULL, xy TEXT NOT NULL, k TEXT, t INTEGER, p REAL, xid TEXT, PRIMARY KEY (u, c, xy))",
  "CREATE TABLE IF NOT EXISTS markmeta (u TEXT NOT NULL, c TEXT NOT NULL, k TEXT, t INTEGER, cs_l TEXT, cs_v REAL, bn_lb INTEGER, bn_sb INTEGER, nat INTEGER, PRIMARY KEY (u, c))",
  "CREATE TABLE IF NOT EXISTS prices (u TEXT NOT NULL, c TEXT NOT NULL, p REAL, PRIMARY KEY (u, c))"
];
let _marksOK = false;
async function marksInit(env) {
  if (!(await d1Init(env))) return false;
  if (_marksOK) return true;
  for (const s of MARKS_TABLES_SQL) await env.DB.prepare(s).run();
  _marksOK = true;
  return true;
}
const DEF_XY = "默认";
// 组装：行 → 与 blob 时代完全同构的 {c: {t,k,cs?,bn?,nat?,xys:{...}}}
async function pubGet(env, u) {
  if (!(await marksInit(env))) return {};
  const out = {};
  const ms = await env.DB.prepare("SELECT c, xy, k, t, p, xid FROM marks WHERE u = ?").bind(u).all();
  const mt = await env.DB.prepare("SELECT c, k, t, cs_l, cs_v, bn_lb, bn_sb, nat FROM markmeta WHERE u = ?").bind(u).all();
  const meta = {};
  for (const m of mt.results || []) meta[m.c] = m;
  for (const r of ms.results || []) {
    const c = r.c;
    if (!out[c]) {
      const m = meta[c] || {};
      out[c] = { t: +m.t || 0, k: m.k === "s" ? "s" : "l", xys: {} };
      if (m.cs_l != null || m.cs_v != null) out[c].cs = { l: m.cs_l || "", v: m.cs_v == null ? null : +m.cs_v };
      if (m.bn_lb != null || m.bn_sb != null) out[c].bn = { lb: m.bn_lb == null ? 0 : +m.bn_lb, sb: m.bn_sb == null ? 0 : +m.bn_sb };
      if (m.nat != null) out[c].nat = +m.nat;
    }
    const e = { t: +r.t || 0, k: r.k === "s" ? "s" : "l" };
    if (r.p != null && isFinite(+r.p)) e.p = +r.p;
    if (r.xid) e.xid = r.xid;
    out[c].xys[r.xy] = e;
  }
  return out;
}
async function pubGetP(env, u) {
  if (!(await marksInit(env))) return {};
  const out = {};
  const rs = await env.DB.prepare("SELECT c, p FROM prices WHERE u = ?").bind(u).all();
  for (const r of rs.results || []) if (r.p != null && isFinite(+r.p)) out[r.c] = +r.p;
  return out;
}
async function pubpSet(env, u, c, p) {              // 单价镜像 upsert；p 非法 = 删
  if (!(await marksInit(env))) return;
  if (p == null || !isFinite(+p)) { await env.DB.prepare("DELETE FROM prices WHERE u = ? AND c = ?").bind(u, c).run(); return; }
  await env.DB.prepare("INSERT INTO prices (u,c,p) VALUES (?,?,?) ON CONFLICT(u,c) DO UPDATE SET p = excluded.p").bind(u, c, +p).run();
}
async function markRowUpsert(env, u, c, xy, e) {    // 一条发布记录 upsert
  if (!(await marksInit(env))) return;
  await env.DB.prepare(
    "INSERT INTO marks (u,c,xy,k,t,p,xid) VALUES (?,?,?,?,?,?,?) ON CONFLICT(u,c,xy) DO UPDATE SET k=excluded.k, t=excluded.t, p=excluded.p, xid=excluded.xid"
  ).bind(u, c, xy, e.k === "s" ? "s" : "l", +e.t || 0,
         (e.p != null && isFinite(+e.p)) ? +e.p : null, e.xid ? String(e.xid).slice(0, 40) : null).run();
}
async function markRowDel(env, u, c, xy) {
  if (!(await marksInit(env))) return;
  await env.DB.prepare("DELETE FROM marks WHERE u = ? AND c = ? AND xy = ?").bind(u, c, xy).run();
}
async function markCardDel(env, u, c) {             // 整卡删除（最后一条取消发布时）
  if (!(await marksInit(env))) return;
  await env.DB.prepare("DELETE FROM marks WHERE u = ? AND c = ?").bind(u, c).run();
  await env.DB.prepare("DELETE FROM markmeta WHERE u = ? AND c = ?").bind(u, c).run();
}
async function markMetaUpsert(env, u, c, m) {       // m = 组装态卡对象（取顶层镜像与 cs/bn/nat）
  if (!(await marksInit(env))) return;
  await env.DB.prepare(
    "INSERT INTO markmeta (u,c,k,t,cs_l,cs_v,bn_lb,bn_sb,nat) VALUES (?,?,?,?,?,?,?,?,?) " +
    "ON CONFLICT(u,c) DO UPDATE SET k=excluded.k, t=excluded.t, cs_l=excluded.cs_l, cs_v=excluded.cs_v, bn_lb=excluded.bn_lb, bn_sb=excluded.bn_sb, nat=excluded.nat"
  ).bind(u, c, m.k === "s" ? "s" : "l", +m.t || 0,
         m.cs ? (m.cs.l || "") : null, m.cs ? (m.cs.v == null ? null : +m.cs.v) : null,
         m.bn ? (isFinite(+m.bn.lb) ? +m.bn.lb : 0) : null, m.bn ? (isFinite(+m.bn.sb) ? +m.bn.sb : 0) : null,
         m.nat == null ? null : +m.nat).run();
}
// 老格式（数字 / {t,k}）包上 xys，价从镜像带进默认号（纯函数，迁移/快照也用）
function normMarkG(v, flatP) {
  if (typeof v !== "object" || !v) v = { t: +v || Date.now(), k: "l" };
  if (!v.xys || typeof v.xys !== "object") {
    const d = { t: +v.t || Date.now(), k: v.k === "s" ? "s" : "l", p: isFinite(+flatP) ? +flatP : null };
    v = { t: d.t, k: d.k, xys: { [DEF_XY]: d } };
  }
  return v;
}
async function markUserDel(env, u) {                 // 删账号时连行级数据一起清
  if (!(await marksInit(env))) return;
  await env.DB.prepare("DELETE FROM marks WHERE u = ?").bind(u).run();
  await env.DB.prepare("DELETE FROM markmeta WHERE u = ?").bind(u).run();
  await env.DB.prepare("DELETE FROM prices WHERE u = ?").bind(u).run();
}
// blob → 行（restore / 迁移用；替换式导入一张卡的所有行 + 镜像）
async function pubImport(env, u, blobMarks, ts) {
  if (!(await marksInit(env))) return;
  for (const [c0, v] of Object.entries(blobMarks || {})) {
    const c = safeKey(c0, 100);
    if (!c) continue;
    const m = normMarkG(v, null);
    if (!(+m.t || 0)) m.t = ts || Date.now();       // 归一化补的时间戳不落库成假数据
    await env.DB.prepare("DELETE FROM marks WHERE u = ? AND c = ?").bind(u, c).run();
    for (const [xy, e] of Object.entries(m.xys || {})) await markRowUpsert(env, u, c, xy, e);
    await markMetaUpsert(env, u, c, m);
  }
}
async function pubpImport(env, u, prices) {
  if (!(await marksInit(env))) return;
  for (const [c0, p] of Object.entries(prices || {})) {
    const c = safeKey(c0, 100);
    if (!c) continue;
    await pubpSet(env, u, c, isFinite(+p) ? +p : null);
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
// ---------------- 密码加固（2026-10-13，免费档方案） ----------------
// 裸 SHA-256(salt+pw) 撞库成本太低 → PBKDF2-SHA256。格式烤进 ph 字段：
//   新格式 "pbkdf2$<iter>$<salt>$<hex>"；老格式就是裸 hex，登录成功时静默升级（不惊动用户）。
// 免费套餐每次请求 10ms CPU：1 万次迭代走 WebCrypto 原生实现实测毫秒级，余量足；
// OWASP 推荐的 21 万次要付费版（默认 30s CPU）才跑得动——哪天升 ¥36 只改下面这一个数。
const PBKDF2_ITER = 10000;
async function pbkdf2hex(p, saltHex, iter) {
  const salt = new Uint8Array(saltHex.match(/.{2}/g).map(h => parseInt(h, 16)));
  const key = await crypto.subtle.importKey("raw", enc.encode(p), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations: iter }, key, 256);
  return [...new Uint8Array(bits)].map(b => b.toString(16).padStart(2, "0")).join("");
}
// 老格式密码 → 新格式（换个新盐；pv 不动，已登录的 token 不掉线）
async function upgradePw(env, users, u, p) {
  const user = users[u];
  if (!user || String(user.ph || "").startsWith("pbkdf2$")) return;
  const salt2 = crypto.randomUUID().replaceAll("-", "");
  user.salt = salt2;
  user.ph = "pbkdf2$" + PBKDF2_ITER + "$" + salt2 + "$" + (await pbkdf2hex(p, salt2, PBKDF2_ITER));
  await putUsers(env, users);
}
async function verifyPw(user, p) {
  const ph = String(user.ph || "");
  if (ph.startsWith("pbkdf2$")) {
    const parts = ph.split("$");                       // [pbkdf2, iter, salt, hex]
    if (parts.length !== 4 || !/^\d+$/.test(parts[1])) return false;
    try { return (await pbkdf2hex(p, parts[2], +parts[1])) === parts[3]; } catch (e) { return false; }
  }
  const h = await sha256hex(user.salt + p);
  return h === ph;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    const path = url.pathname;
    // 维护开关（2026-10-15）：KV 键 "maintenance" = "1" 时断所有用户读写（存储手术/升级用）。
    // 只放行 /appcheck、/appdl（无副作用的版本检查与下载）；其余一律 503，客户端显示维护中。
    // 解除：删掉 KV 里的 maintenance 键即可（运维口 /admin/restore 同权限级别，不暴露到前端）。
    if (path !== "/appcheck" && path !== "/appdl") {
      try {
        if ((await env.KV.get("maintenance")) === "1")
          return jerr("系统维护中（正在升级存储），请几分钟后重试", 503);
      } catch (e) {}
    }
    try {
      if (ctx) ctx.waitUntil(dailySnap(env));   // 每日 users 快照（当天首次请求触发，不挡响应）
      // 全路由串行化（审计 G15）：读改写型操作（pub/pubsync/admin/save/myimgs…）不再有并发丢更新
      return await withLock(() => route(request, env, path, ctx));
    }
    catch (e) { return jerr("服务异常：" + e.message, 500); }
  }
};

// 每日快照（2026-10-13 起做 users；2026-10-14 体检 P1-D4 扩到业务核心；2026-10-24 扩 myimgs）：
// 当天第一个请求把 users + 全部 pub_/pubp_/xy_ + myimgs 图池整包
// 写进 snap_YYYYMMDD，只留最近 7 份（按键名日期清旧的）。写量 2 次/天，额度无压力。
// myimgs 已 D1 行级化（myimg_* 行），快照时组装回整包 ~9MB < KV 25MB 单值上限。
// 兼容：旧快照是 users 裸串、新快照是 {users:..., pub_B:...} 对象，恢复口两样都认。
// 就算忘了手动 /admin/backup，也永远有一周的滚动恢复点。
let _snapDay = null;
async function dailySnap(env) {
  const today = new Date().toISOString().slice(0, 10).replaceAll("-", "");
  if (_snapDay === today) return;
  _snapDay = today;
  try {
    if ((await env.KV.get("snap_last")) === today) return;
    const users = await env.KV.get(K_USERS);
    if (users == null) return;                        // 还没 init 过，没得备
    const snap = { users };
    for (const pre of ["xy_"]) {                      // xy_ 留 KV（低频写）；pub_/pubp_ 已迁 D1，从下面补进来
      const lst = await env.KV.list({ prefix: pre });
      for (const k of (lst.keys || [])) {
        try {
          const v = await env.KV.get(k.name);
          if (v != null) snap[k.name] = v;
        } catch (e) {}                                // 单键读失败跳过，别拖垮整个快照
      }
    }
    try {
      for (const u of Object.keys(JSON.parse(users))) {   // pub_/pubp_ 已行级化：按账号现组装成 blob 形态进快照
        try { snap["pub_" + u] = JSON.stringify(await pubGet(env, u)); } catch (e) {}
        try { snap["pubp_" + u] = JSON.stringify(await pubGetP(env, u)); } catch (e) {}
      }
    } catch (e) { console.error("快照取行级数据失败（降级只备 KV）：" + e.message); }
    // myimgs 图池进快照（2026-10-24）：D1 行级读全量组装，整包 ~9MB < KV 25MB 单值上限
    try {
      const mi = await myimgsGet(env);
      if (Object.keys(mi).length) snap["myimgs"] = JSON.stringify(mi);
    } catch (e) { console.error("快照收 myimgs 失败（跳过）：" + e.message); }
    await env.KV.put("snap_" + today, JSON.stringify(snap));
    await env.KV.put("snap_last", today);
    const cutoff = new Date(Date.now() - 8 * 86400000).toISOString().slice(0, 10).replaceAll("-", "");
    const lst = await env.KV.list({ prefix: "snap_" });
    for (const k of (lst.keys || [])) {
      const d = k.name.slice(5);
      if (/^\d{8}$/.test(d) && d <= cutoff) await env.KV.delete(k.name);   // snap_last 不匹配 8 位日期，天然不动
    }
  } catch (e) { console.error("每日快照失败：" + e.message); }
}

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
      ph: "pbkdf2$" + PBKDF2_ITER + "$" + salt + "$" + (await pbkdf2hex(env.ADMIN_PASS, salt, PBKDF2_ITER)),
      salt, role: "admin", note: "管理号", disabled: false, perm: {}, pv: 0,
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
    /* 密码加固（2026-10-13）：老格式（裸 SHA-256）登录成功这一次静默升级成 PBKDF2，用户无感 */
    if (!String(user.ph || "").startsWith("pbkdf2$")) {
      try { await upgradePw(env, users, u, p); } catch (e) { console.error("密码升级失败：" + e.message); }
    }
    const token = await makeToken(env, u, user.pv);
    return J({ ok: true, token, u, role: user.role || "sub",
               perm: user.role === "admin" ? {} : migratePerm(user.perm) });
  }

  // ---------- 手机版 H5（2026-10-12）：/m 出页面（免登录，登录框在页面里） ----------
  if (path === "/m" && request.method === "GET") {
    const html = await env.KV.get("mhtml");
    if (!html) return jerr("手机版还没上云", 404);
    return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8", ...CORS } });
  }

  // 2026-10-13 手机端真软件化：CI 打 APK 时用 X-Cloud-Key 烤数据快照进包
  // （正常用户走下面登录态的同路径，互不影响）
  if (path === "/mpayload" && request.method === "GET") {
    const ck = env.CLOUD_KEY || env.SECRET;
    if (ck && (request.headers.get("X-Cloud-Key") || "") === ck) {
      const v0 = await env.KV.get("mpayload");
      if (!v0) return jerr("手机端数据还没上云", 404);
      return new Response(v0, { headers: { "Content-Type": "application/json; charset=utf-8", ...CORS } });
    }
  }

  // ---------- 救火通道（2026-10-13）：users 账本整包恢复 ----------
  // POST /admin/restore，鉴权走 X-Cloud-Key（CI 同款，范本就是上面的 /mpayload）。
  // 故意不用登录态：要用它的场景恰恰是「账本坏了谁都登不进去」，要求先登录等于
  // 把钥匙锁在屋里。配套 GET /admin/backup（登录态，在下面门禁之后）导出快照。
  if (path === "/admin/restore" && request.method === "POST") {
    const ck = env.CLOUD_KEY || env.SECRET;
    if (!ck || (request.headers.get("X-Cloud-Key") || "") !== ck) return jerr("没有权限", 403);
    const snap = await request.json().catch(() => null);
    if (!snap || typeof snap !== "object" || Array.isArray(snap))
      return jerr("body 应为 users 对象，或每日快照整包 {users:..., pub_B:...}");
    // 2026-10-14 兼容两种快照形态：旧=users 裸对象；新={users:..., pub_/pubp_/xy_ 各键}
    let usersObj = snap, extra = null;
    if (snap.users != null) {
      try {
        usersObj = typeof snap.users === "string" ? JSON.parse(snap.users) : snap.users;
      } catch (e) { return jerr("快照 users 字段解析失败"); }
      extra = snap;
    }
    if (!usersObj || typeof usersObj !== "object" || Array.isArray(usersObj))
      return jerr("快照里没有合法的 users 对象");
    for (const [u0, x] of Object.entries(usersObj)) {
      if (!String(u0).trim() || !x || typeof x !== "object" ||
          !String(x.ph || "") || !String(x.salt || ""))
        return jerr("快照里「" + u0 + "」缺 ph/salt，拒绝整包恢复（不覆盖现有账本）");
    }
    await env.KV.put(K_USERS, JSON.stringify(usersObj));
    await env.KV.put("inited", "1");
    let nBiz = 0;
    if (extra) {
      for (const [k, v] of Object.entries(extra)) {
        if (k === "users") continue;
        if (k === "myimgs") {
          // 2026-10-24：myimgs 图池恢复（写 R2 主存；R2 未绑退 KV）
          try {
            const o = typeof v === "string" ? JSON.parse(v) : v;
            if (o && typeof o === "object" && !Array.isArray(o)) { await myimgsPut(env, o); nBiz++; }
          } catch (e) {}
          continue;
        }
        if (!/^(pub_|pubp_|xy_)/.test(k) || typeof v !== "string") continue;
        try {
          // 2026-10-15 行级化：恢复口把 blob 拆成行导入（marks/markmeta/prices），不再整包覆盖
          if (/^pubp_/.test(k)) await pubpImport(env, k.slice(5), JSON.parse(v));
          else if (/^pub_/.test(k)) await pubImport(env, k.slice(4), JSON.parse(v));
          else await env.KV.put(k, v);                                        // xy_ 留 KV
          nBiz++;
        } catch (e) {}
      }
    }
    return J({ ok: true, msg: "已恢复 " + Object.keys(usersObj).length + " 个账号"
      + (nBiz ? " + " + nBiz + " 个业务键（发布/定价/闲鱼号）" : "") + "，inited 已重设" });
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
    await upgradePw(env, users, who, np);   // 新密码直接存 PBKDF2 新格式（2026-10-13）
    user.pv = (user.pv || 0) + 1;      // 审计（静态2）：密码版本号 +1，旧 token 全部作废，客户端自动重新登录
    await putUsers(env, users);
    return J({ ok: true });
  }

  // ---------- 救火通道（2026-10-13）：users 账本整包导出 ----------
  // GET /admin/backup，管理号登录态。导出的文件配合 POST /admin/restore 用；
  // 平时也可以直接对着每日快照 snap_* 键（见 dailySnap）手动抄。
  if (path === "/admin/backup" && request.method === "GET") {
    const me = usersGate[who];
    if (!me || me.role !== "admin") return jerr("只有管理号能导出", 403);
    return new Response(JSON.stringify(usersGate, null, 2), {
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Disposition": 'attachment; filename="users-backup-' + new Date().toISOString().slice(0, 10) + '.json"',
        ...CORS,
      },
    });
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
      await d1Del(env, "pub_" + u);      // 行级化后这里只是残留备份 blob，一并清
      await d1Del(env, "pubp_" + u);
      await markUserDel(env, u);         // 行级三表按账号清
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
  // 2026-10-15 行级化：读写已搬到顶层行级助手（marks/markmeta/prices 三表），这里只是薄壳。
  // 数据形态与 blob 时代同构：{型号: {t,k,cs?,bn?,nat?,xys:{闲鱼号|口径: {t,k,p,xid?}}}}
  const DEF_XY = "默认";
  const pubTs = (v) => (typeof v === "object" && v ? (+v.t || 0) : (+v || 0));
  async function getPub(u) {
    return pubGet(env, u);
  }
  async function getPubP(u) {
    return pubGetP(env, u);
  }
  // 顶层镜像重算：取 xys 里最新一条的 t/k/p
  function markTop(m) {
    let lt = 0, lk = "l", lp = null;
    for (const e of Object.values(m.xys)) {
      const tt = +e.t || 0;
      if (tt >= lt) { lt = tt; lk = e.k === "s" ? "s" : "l"; lp = isFinite(+e.p) ? +e.p : null; }
    }
    m.t = lt || Date.now(); m.k = lk;
    return m;
  }
  // 老格式（数字 / {t,k}）包上 xys，价从 pubp_ 带进默认号
  function normMark(v, flatP) {
    if (typeof v !== "object" || !v) v = { t: +v || Date.now(), k: "l" };
    if (!v.xys || typeof v.xys !== "object") {
      const d = { t: +v.t || Date.now(), k: v.k === "s" ? "s" : "l", p: isFinite(+flatP) ? +flatP : null };
      v = { t: d.t, k: d.k, xys: { [DEF_XY]: d } };
    }
    return v;
  }
  async function getPubN(u) {
    const m = await getPub(u), pp = await getPubP(u), out = {};
    for (const [c, v] of Object.entries(m)) out[c] = normMark(v, pp[c]);
    return out;
  }

  // 自己的发布记录（带各自的闲鱼定价）
  if (path === "/pub" && request.method === "GET") {
    return J({ ok: true, marks: await getPubN(who), prices: await getPubP(who) });
  }
  // 标记/取消一条：{c, on, k: 'l'|'s', p: 闲鱼定价(可选), xy: 闲鱼号(可选，缺省=默认), cs: {l,v} 成本口径(可选，2026-10-13)}
  //   2026-10-14 管理号代管：带 u=子账号 → 改 TA 的发布记录（仅管理号）；
  //   ponly=true → 只改价不动口径/时间；xidonly=true → 只写/删闲鱼ID
  if (path === "/pub" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const c = safeKey(b.c, 100);                       // 审计（G1/S4）：长度上限 + 键名黑名单
    if (!c) return jerr("型号不能为空");
    let target = who;
    if (b.u) {
      if (usersGate[who].role !== "admin") return jerr("只有管理号能改别人的发布记录", 403);
      const tu = safeKey(b.u, 24);
      if (!tu) return jerr("用户名不合法");
      target = tu;
    }
    const xy = safeKey(b.xy, 24) || DEF_XY;
    const marks = await getPub(target);                // 行级组装（读便宜，写才是原子的）
    if (b.on === false) {
      const cur = marks[c];
      // 指了号且还有多条 → 只删那一条；否则整条删（老客户端行为不变）
      if (cur && typeof cur === "object" && cur.xys && Object.keys(cur.xys).length > 1 && cur.xys[xy]) {
        delete cur.xys[xy];
        markTop(cur);
        marks[c] = cur;
        await markRowDel(env, target, c, xy);          // 行级：只删那一行
        await markMetaUpsert(env, target, c, cur);     // 顶层镜像跟新
        // pubp_ 镜像跟到剩余最新一条的价（没价就删镜像）
        const rest = Object.values(cur.xys).sort((a, z) => (+z.t || 0) - (+a.t || 0))[0] || null;
        await pubpSet(env, target, c, rest && isFinite(+rest.p) ? +rest.p : null);
      } else {
        await markCardDel(env, target, c);             // 行级：整卡删除（记录行 + 镜像行）
        await pubpSet(env, target, c, null);           // 2026-10-10 新规矩：取消发布，定价一并清掉
      }
    } else {
      const now = Date.now();
      const k = b.k === "s" ? "s" : "l";
      const cur = normMark(marks[c], (await getPubP(target))[c]);
      // 只删/写闲鱼ID（2026-10-14）：不动口径、不动时间、不动价
      if (b.xidonly) {
        const e = cur.xys[xy];
        if (!e) return jerr("没有这条发布记录");
        if (b.xid) e.xid = String(b.xid).trim().slice(0, 40); else delete e.xid;
        await markRowUpsert(env, target, c, xy, e);    // 行级：只动这一行的 xid
        return J({ ok: true });
      }
      // 只改价（2026-10-14 管理号在子用户发布视图里改价）：不动口径、不动记录时间
      if (b.ponly) {
        const keys = Object.keys(cur.xys);
        if (!keys.length) return jerr("该型号还没有发布记录");
        const e = cur.xys[xy] || keys.map(x => cur.xys[x]).sort((a, z) => (+z.t || 0) - (+a.t || 0))[0];
        if (!isFinite(+b.p)) return jerr("价格不对");
        e.p = +b.p;
        await markRowUpsert(env, target, c, xy, e);    // 行级：只动这一行的价
        const lt = Object.values(cur.xys).sort((a, z) => (+z.t || 0) - (+a.t || 0))[0];
        await pubpSet(env, target, c, lt && isFinite(+lt.p) ? +lt.p : null);
        return J({ ok: true });
      }
      // 成本口径跟发布记录走（2026-10-13 用户定稿）：搭车存，旧客户端发的没这字段自动跳过
      if (b.cs && typeof b.cs === "object" && !Array.isArray(b.cs))
        cur.cs = { l: safeKey(b.cs.l, 40) || "", v: isFinite(+b.cs.v) ? +b.cs.v : null };
      // 2026-10-13 修「价格乱跳」：请求没带 p（客户端本地没价，绝不能发 null 抹价）
      // → 保留该号现价；明确带了数字才盖，显式 null 才清（弹窗必填价路径不受影响）
      const oldE = cur.xys[xy];
      const np = b.p === undefined
        ? (oldE && isFinite(+oldE.p) ? +oldE.p : null)
        : (isFinite(+b.p) ? +b.p : null);
      // 闲鱼ID（2026-10-13）：带了就存（截40位），没带保留这条原本的——改价/改口径不抹ID
      const nxid = b.xid === undefined
        ? (oldE && oldE.xid != null ? String(oldE.xid).slice(0, 40) : null)
        : (b.xid ? String(b.xid).trim().slice(0, 40) : null);
      const ne = { t: now, k, p: np };
      if (nxid) ne.xid = nxid;
      cur.xys[xy] = ne;
      markTop(cur);
      marks[c] = cur;
      await markRowUpsert(env, target, c, xy, ne);     // 行级：这一行 upsert（原子）
      await markMetaUpsert(env, target, c, cur);       // 顶层镜像 + cs/bn/nat
      if (isFinite(np)) await pubpSet(env, target, c, +np);
    }
    return J({ ok: true });
  }
  // 整包合并（登录后把本机旧记录并上云端）：{marks: {c: {t,k} | {t,k,xys}}, prices: {c: 价}, xyp: {c: {号: 价}}}
  if (path === "/pubsync" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    if (Array.isArray(b.prices)) return jerr("prices 格式错误（应为对象）");   // 审计：数组下标会被当型号
    const marks = await getPub(who);                   // 行级组装（读便宜，写按行原子）
    const pp0 = await getPubP(who);
    const inc = b.marks && typeof b.marks === "object" && !Array.isArray(b.marks) ? b.marks : {};
    for (const [c0, v] of Object.entries(inc)) {
      const c = safeKey(c0, 100);
      if (!c) continue;                                // 审计（S4）：脏键直接丢
      if (v && typeof v === "object" && v.xys && typeof v.xys === "object" && Object.keys(v.xys).length) {
        // 新客户端：逐号并（只新盖旧，不删云端已有号）
        const cur = normMark(marks[c], pp0[c]);
        let touched = false;
        for (const [x0, e] of Object.entries(v.xys)) {
          const x = safeKey(x0, 24);
          if (!x || !e || typeof e !== "object") continue;
          if (!cur.xys[x] || (+cur.xys[x].t || 0) < (+e.t || 0)) {
            const ne = { t: +e.t || Date.now(), k: e.k === "s" ? "s" : "l", p: isFinite(+e.p) ? +e.p : null };
            if (e.xid) ne.xid = String(e.xid).trim().slice(0, 40);   // 闲鱼ID跟着记录走（2026-10-13）
            cur.xys[x] = ne;
            await markRowUpsert(env, who, c, x, ne);   // 行级：一条一行，天然原子
            touched = true;
          }
        }
        if (v.cs && typeof v.cs === "object" && !Array.isArray(v.cs))
          cur.cs = { l: safeKey(v.cs.l, 40) || "", v: isFinite(+v.cs.v) ? +v.cs.v : null };
        if (touched || v.cs) {
          markTop(cur);
          marks[c] = cur;
          await markMetaUpsert(env, who, c, cur);      // 顶层镜像 + cs 跟行级走
        }
      } else {
        // 老客户端：并进默认号
        const t = pubTs(v) || Date.now();
        const k = typeof v === "object" && v ? (v.k === "s" ? "s" : "l") : "l";
        const cur = normMark(marks[c], pp0[c]);
        const d = cur.xys[DEF_XY];
        if (!d || (+d.t || 0) < t) {
          const ne = { t, k, p: isFinite(+pp0[c]) ? +pp0[c] : null };
          cur.xys[DEF_XY] = ne;
          markTop(cur);
          marks[c] = cur;
          await markRowUpsert(env, who, c, DEF_XY, ne);
          await markMetaUpsert(env, who, c, cur);
        }
      }
    }
    // cs/bn/nat 通道（2026-10-13）：口径 cs、出库奖励点击状态 bn、发全国勾选 nat
    // 跟 markmeta 表走，只动各自字段，不碰 xys/时间戳/pubp_。行级天然全归一化，无「老格式补写」之说
    let chC = false;
    if (b.cs && typeof b.cs === "object" && !Array.isArray(b.cs)) {
      for (const [c0, v] of Object.entries(b.cs)) {
        const c = safeKey(c0, 100);
        if (!c || !v || typeof v !== "object" || Array.isArray(v)) continue;
        const cur = marks[c];
        if (!cur || typeof cur !== "object") continue;             // 只改已发布的
        const ncs = { l: safeKey(v.l, 40) || "", v: isFinite(+v.v) ? +v.v : null };
        if (JSON.stringify(cur.cs || {}) !== JSON.stringify(ncs)) { cur.cs = ncs; await markMetaUpsert(env, who, c, cur); chC = true; }
      }
    }
    if (b.bn && typeof b.bn === "object" && !Array.isArray(b.bn)) {
      for (const [c0, v] of Object.entries(b.bn)) {
        const c = safeKey(c0, 100);
        if (!c || !v || typeof v !== "object" || Array.isArray(v)) continue;
        const cur = marks[c];
        if (!cur || typeof cur !== "object") continue;
        const nb = { lb: isFinite(+v.lb) ? +v.lb : 0, sb: isFinite(+v.sb) ? +v.sb : 0 };
        if (JSON.stringify(cur.bn || {}) !== JSON.stringify(nb)) { cur.bn = nb; await markMetaUpsert(env, who, c, cur); chC = true; }
      }
    }
    if (b.nat && typeof b.nat === "object" && !Array.isArray(b.nat)) {
      for (const [c0, v] of Object.entries(b.nat)) {
        const c = safeKey(c0, 100);
        if (!c || v == null) continue;
        const cur = marks[c];
        if (!cur || typeof cur !== "object") continue;
        const nv = +v === 1 ? 1 : 0;
        if (cur.nat !== nv) { cur.nat = nv; await markMetaUpsert(env, who, c, cur); chC = true; }
      }
    }
    // xyp 增量（2026-10-12 多闲鱼号）：改某号某型的价，只对已存在的标记改，不造新标记
    let chPP = false;   // pubp_ 镜像有变更（2026-10-14 体检 P1-1：xyp 改价也要跟镜像）
    if (b.xyp && typeof b.xyp === "object" && !Array.isArray(b.xyp)) {
      const xypC = {};   // 有价变更的型号 → 镜像要重算
      for (const [c0, xs] of Object.entries(b.xyp)) {
        const c = safeKey(c0, 100);
        if (!c || !xs || typeof xs !== "object" || Array.isArray(xs)) continue;
        const cur = marks[c];
        if (!cur || typeof cur !== "object") continue;
        for (const [x0, p] of Object.entries(xs)) {
          const x = safeKey(x0, 24);
          if (!x || !cur.xys[x] || !isFinite(+p) || cur.xys[x].p === +p) continue;
          cur.xys[x].p = +p;
          await markRowUpsert(env, who, c, x, cur.xys[x]);   // 行级：只动这一行的价
          xypC[c] = true;
        }
      }
      // 2026-10-14 修「手机端价格漂移」：镜像 = 该型号最新一条记录的价
      for (const c of Object.keys(xypC)) {
        const lt = Object.values(marks[c].xys || {}).sort((a, z) => (+z.t || 0) - (+a.t || 0))[0];
        if (lt && isFinite(+lt.p) && pp0[c] !== +lt.p) { pp0[c] = +lt.p; await pubpSet(env, who, c, +lt.p); chPP = true; }
      }
    }
    if (b.prices && typeof b.prices === "object" && !Array.isArray(b.prices)) {
      for (const [c0, p] of Object.entries(b.prices)) {
        const c = safeKey(c0, 100);
        if (!c) continue;
        // 2026-10-08：全量收价 —— 没点「标记发布」的型号定价也要存（用户要求）
        // 2026-10-16 连根修（B）：已发布型号**记录价优先**——定价表跟最新一条发布记录走，
        // 客户端旧定价表不再把改过的闲鱼价盖回去（LC-172/CWY16 亏钱误报案的根因）。
        // 没发布记录（或记录没价）才收客户端的价，「草稿价也要落库」老规矩不变
        let rp = null;
        const mrc = marks[c];
        if (mrc && mrc.xys) {
          const lt = Object.values(mrc.xys).sort((a, z) => (+z.t || 0) - (+a.t || 0))[0];
          if (lt && isFinite(+lt.p)) rp = +lt.p;
        }
        const fin = rp != null ? rp : (isFinite(+p) ? +p : null);
        if (fin != null && pp0[c] !== fin) { pp0[c] = fin; await pubpSet(env, who, c, fin); chPP = true; }
      }
    }
    // 2026-10-16 连根修（B）：全量对账——已发布且有价的型号，定价表一律对齐最新记录价。
    // 客户端 prices 没带的键、或历史 drift（记录 6999 / 定价表 2999），这次同步全部治愈。
    // 记录没价的不动（留给客户端草稿价走上面的通道）
    for (const [c, mrc] of Object.entries(marks)) {
      if (!mrc || !mrc.xys) continue;
      const lt = Object.values(mrc.xys).sort((a, z) => (+z.t || 0) - (+a.t || 0))[0];
      if (lt && isFinite(+lt.p) && pp0[c] !== +lt.p) { pp0[c] = +lt.p; await pubpSet(env, who, c, +lt.p); chPP = true; }
    }
    // 2026-10-14 体检 P1-4：返回直接用内存组装（marks/pp0 都在手上），
    // 省掉结尾 getPubN+getPubP 的 2 次重复读——免费版 KV 读额度的大头之一
    const outM = {};
    for (const [c, v] of Object.entries(marks)) outM[c] = normMark(v, pp0[c]);
    return J({ ok: true, marks: outM, prices: pp0 });
  }
  // 管理号看所有**子账号**各发布了什么（带各自的闲鱼定价；管理号自己不在这份名单里）
  if (path === "/puball" && request.method === "GET") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号能看", 403);
    const out = {}, prices = {};
    for (const u of Object.keys(users)) {
      if (users[u].role === "admin") continue;
      out[u] = await getPubN(u);
      prices[u] = await getPubP(u);
    }
    return J({ ok: true, all: out, prices });
  }

  // ---------- 闲鱼号配置（2026-10-13 用户定稿）----------
  // KV: xy_<u> = ["默认","小号A",...] 每账号自己名下一份。
  // 自己 GET/POST 自己的；?u= 别人的管理号可看可改（账号管理面板代设）。
  async function getXy(u) { return getJSON(env, "xy_" + u, []); }
  if (path === "/xy" && request.method === "GET") {
    const u = url.searchParams.get("u") || who;
    if (u !== who) {
      const me = (await getUsers(env))[who];
      if (!me || me.role !== "admin") return jerr("只有管理号能看别人的闲鱼号", 403);
    }
    return J({ ok: true, u, list: await getXy(u) });
  }
  if (path === "/xy" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    let u = who;
    if (b.u && b.u !== who) {
      const me = (await getUsers(env))[who];
      if (!me || me.role !== "admin") return jerr("只有管理号能改别人的闲鱼号", 403);
      u = safeKey(b.u, 40);
      if (!u) return jerr("账号名不合法");
    }
    if (!Array.isArray(b.list)) return jerr("list 格式错误");
    const list = b.list.map((x) => String(x || "").trim().slice(0, 24)).filter(Boolean).slice(0, 20);
    await env.KV.put("xy_" + u, JSON.stringify(list));
    return J({ ok: true, u, list });
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
    for (const k of keys) {
      if (k in pp) { delete pp[k]; removed.push(k); await pubpSet(env, u, k, null); }
    }
    return J({ ok: true, u, removed, prices: pp });
  }

  // ---------- 补图上云（所有人共享：谁补的大家都能看到） ----------
  // 2026-10-24 D1 行级化：myimgsGet/myimgsPut 搬到顶层（一图一行 myimg_*，见文件头部 D1 区）。
  // 格式与原 KV 键完全一致（{卡片key: {b, by}}），客户端协议零改动。
  // KV 键 "myimgs" 只作惰性迁移源，迁移完成后可手动清掉。
  // KV 时代注释：myimgs = { 卡片key: {b: dataURL, by: 补图人|'sys'(整库灌入)|'__legacy__'(旧数据)} }
  // 2026-10-12 加归属：公共池合并拉下来后人人本地都有，原来 × 谁都能点、谁都能删别人的图。
  async function getMyImgs(env) {
    const raw = await myimgsGet(env);
    const out = {};
    for (const [k, v] of Object.entries(raw)) {
      if (typeof v === "string") out[k] = { b: v, by: "__legacy__" };   // 旧格式兜一层
      else if (v && typeof v === "object" && v.b) out[k] = { b: v.b, by: v.by || "__legacy__" };
    }
    return out;
  }
  if (path === "/myimgs" && request.method === "GET") {
    return J({ ok: true, imgs: await getMyImgs(env) });
  }
  if (path === "/myimg" && request.method === "POST") {
    const b = await request.json().catch(() => ({}));
    const c = safeKey(b.c, 100);                       // 审计（S4）：键名黑名单
    if (!c || !b.b64) return jerr("参数不全");
    // 2026-10-24 管理号隐藏标记：b64==='hide' → 存 {b:'hide',by:'hide'}，全端 syncImgs 收敛压图
    const isHide = b.b64 === "hide";
    if (isHide) {
      const users = await getUsers(env);
      const me = users[who];
      if (!me || me.role !== "admin") return jerr("只有管理号能隐藏图", 403);
    } else if (String(b.b64).length > 400000) return jerr("图太大，压一压再传");
    const imgs = await getMyImgs(env);
    imgs[c] = isHide ? { b: "hide", by: "hide" } : { b: String(b.b64), by: who };  // 归属 = 上传人（服务端定，不信客户端）
    const total = JSON.stringify(imgs).length;
    if (total > 24000000) return jerr("云端补图库满了（" + Math.round(total/1048576) + "MB），清理后再传");
    await myimgsPut(env, imgs);
    return J({ ok: true, total });
  }
  if (path === "/myimg" && request.method === "DELETE") {
    const b = await request.json().catch(() => ({}));
    const c = safeKey(b.c, 100);
    if (!c) return jerr("型号不能为空");
    const imgs = await getMyImgs(env);
    const e = imgs[c];
    if (e) {
      // 2026-10-12 权限：子账号只能删自己补的；管理号除 sys 整库图外都能删
      const users = await getUsers(env);
      const isAdmin = !!(users[who] && users[who].role === "admin");
      if (e.by !== who && !(isAdmin && e.by !== "sys")) return jerr("只能删自己补的图", 403);
      delete imgs[c];
      await myimgsPut(env, imgs);
    }
    return J({ ok: true });
  }
  // 整库一次性上云（2026-10-12）：管理号把本地 payload 图库全量灌进 myimgs。
  // 一次 KV.put = 1 写额度（逐图 POST 会烧上千写/天）；单值上限 25MB，超了拒收。
  if (path === "/myimgsbulk" && request.method === "POST") {
    const users = await getUsers(env);
    const me = users[who];
    if (!me || me.role !== "admin") return jerr("只有管理号能整库上云", 403);
    const b = await request.json().catch(() => ({}));
    const inc = b.imgs && typeof b.imgs === "object" && !Array.isArray(b.imgs) ? b.imgs : null;
    if (!inc) return jerr("imgs 字段不对");
    const imgs = await getMyImgs(env);
    let n = 0;
    for (const c0 of Object.keys(inc)) {
      const c = safeKey(c0, 100);
      const v = inc[c0];
      if (!c || typeof v !== "string" || !v.startsWith("data:image/")) continue;
      if (v.length > 400000) continue;
      imgs[c] = { b: v, by: "sys" }; n++;              // 整库灌入的标 sys，谁都不能在前端删
    }
    const total = JSON.stringify(imgs).length;
    if (total > 24000000) return jerr("整库太大（" + Math.round(total / 1048576) + "MB），超 KV 单值上限");
    await myimgsPut(env, imgs);
    return J({ ok: true, added: n, total: Object.keys(imgs).length, bytes: total });
  }

  // ---------- 手机版数据 /mpayload（2026-10-12）----------
  // GET：登录用户读手机端 payload（{meta,rows,imgs}，CI 每轮金山任务重建上传）。
  // POST：管理号/C-I 上传；内容没变不写 KV（省写额度）；单值上限 24MB。
  if (path === "/mpayload" && request.method === "GET") {
    const v = await env.KV.get("mpayload");
    if (!v) return jerr("手机端数据还没上云", 404);
    return new Response(v, { headers: { "Content-Type": "application/json; charset=utf-8", ...CORS } });
  }
  if (path === "/mpayload" && request.method === "POST") {
    const users0 = await getUsers(env);
    if ((users0[who] || {}).role !== "admin") return jerr("只有管理号能上传手机端数据", 403);
    const raw = await request.text();
    if (raw.length > 24000000) return jerr("手机端数据太大（" + Math.round(raw.length / 1048576) + "MB），超 KV 单值上限", 413);
    let obj; try { obj = JSON.parse(raw); } catch (e) { return jerr("不是合法 JSON"); }
    if (!obj || !Array.isArray(obj.rows)) return jerr("缺 rows，拒收");
    const mb = Math.round(raw.length / 1048576 * 10) / 10;
    if ((await env.KV.get("mpayload")) === raw) return J({ ok: true, changed: false, mb });
    await env.KV.put("mpayload", raw);
    return J({ ok: true, changed: true, rows: obj.rows.length, mb });
  }

  // ---------- 手机版页面 /mhtml（2026-10-12）：管理号更新 KV 里的 m.html ----------
  if (path === "/mhtml" && request.method === "POST") {
    const users0 = await getUsers(env);
    if ((users0[who] || {}).role !== "admin") return jerr("只有管理号能更新手机版页面", 403);
    const b = await request.json().catch(() => ({}));
    if (!b || typeof b.html !== "string" || !b.html) return jerr("html 字段不对");
    if (b.html.length > 1000000) return jerr("html 太大（" + Math.round(b.html.length / 1024) + "KB）");
    await env.KV.put("mhtml", b.html);
    return J({ ok: true, kb: Math.round(b.html.length / 1024) });
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
    return J({ ok: true, ts: obj.ts || "", rows: obj.rows || 0, by: obj.by || "", up: +obj.up || 0 });   // up=最近成功跑批（本机哨兵用，2026-10-24）
  }

  // 跑批报到（2026-10-24）：kdocs CI 内容没变时跳过文件上传 → up 不动，本机哨兵会误报停摆。
  // 每轮跑完都打一下这个口子只刷 up（KV 写 ~15 次/天，额度无压力）。X-Cloud-Key 同 /mpayload 通道。
  if (path === "/xlsxping" && request.method === "POST") {
    const ck = env.CLOUD_KEY || env.SECRET;
    if (!ck || (request.headers.get("X-Cloud-Key") || "") !== ck) return jerr("没有权限", 403);
    const obj = await getJSON(env, "xlsxlive", null);
    if (!obj) return J({ ok: true });          // 云端还没有过上传，无需报到
    obj.up = Date.now();
    await env.KV.put("xlsxlive", JSON.stringify(obj));
    return J({ ok: true });
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
    return J({ ok: true, ts: obj.ts || "", by: obj.by || "", up: +obj.up || 0 });   // up=最近成功跑批时间戳（本机哨兵用，2026-10-24）
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
    // 2026-10-13 手机 APP「检查更新」：?plat=apk 匹配 ModelLookup-Mobile-*.apk
    if (plat === "apk" || plat === "android") {
      return (rel.assets || []).find((x) => /^ModelLookup-Mobile-[\w.]*\.apk$/i.test(x.name))
          || (rel.assets || []).find((x) => /\.apk$/i.test(x.name)) || null;
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
    // 2026-10-14 体检 P0-3：透传 GitHub 资产的 SHA-256（GitHub 自动算），客户端下载后
    // 流式校验——更新链上任一环被篡改，装包前就能拦下。旧资产没有 digest 字段时不带。
    const dg = (a && typeof a.digest === "string" && a.digest.indexOf("sha256:") === 0)
      ? a.digest.slice(7) : "";
    return J({ ok: true, tag: rel.tag_name, ver: String(rel.tag_name).replace(/^v/i, ""),
               name: a ? a.name : "", size: a ? a.size : 0, sha256: dg || undefined });
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
