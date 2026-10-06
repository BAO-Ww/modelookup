# -*- coding: utf-8 -*-
"""
型号查询 · 隆阳电器（公布版）
================================
独立窗口的桌面软件（自带 WebView2 内核，无地址栏 / 无浏览器外壳）：

  · 双击 → 弹出一个自己的窗口，里面就是查询界面（数据已内嵌，秒开）
  · 页面上 / 菜单里「从金山文档更新」→ 自动拉最新在线表 → 重建 → 自动刷新
      · 已登录：后台静默完成，窗口都不闪
      · 未登录：弹出「登录金山文档」窗口，扫码后自动继续
  · 也支持把从金山文档下载的 xlsx 拖到 exe 图标上重建

自检:  型号查询.exe --check        （结果写到 exe 同目录 exe_check.log）
"""
import base64
import io
import json
import os
import re
import ssl
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ⚠ 顶层 import（不能放进函数里）：放进函数体内会被 PyInstaller 判定为「延迟/可选」而不打包
import openpyxl  # noqa: E402
import webview  # noqa: E402

APP_NAME = "型号查询"
APP_VERSION = "2.0"
APP_ID = "ModelLookup"

ONLINE_SHARE = "https://www.kdocs.cn/l/ch7gwGTP9De4"
FILE_SHARE = "ch7gwGTP9De4"
FILE_ID = "548020345856"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# ------------------------------------------------------------------ 路径

def is_frozen():
    return bool(getattr(sys, "frozen", False))


def res_dir():
    """打包后的资源目录 / 开发时的 assets 目录。"""
    if is_frozen():
        return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(sys.executable)), "assets")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")


def work_dir():
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_ID)
    os.makedirs(d, exist_ok=True)
    return d


def rp(name):
    return os.path.join(res_dir(), name)


def wp(name):
    return os.path.join(work_dir(), name)


# ------------------------------------------------------------------ 归一化（与前端严格一致）

def norm_model(v):
    t = str(v or "")
    out = []
    for ch in t:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            ch = chr(o - 0xFEE0)
        elif ch == "\u3000":
            ch = " "
        out.append(ch)
    return re.sub(r"\s+", "", "".join(out)).upper()


def core_of(v):
    n = norm_model(v)
    m = re.search(r"[\u4e00-\u9fff]", n)
    if m and m.start() >= 3:
        return n[: m.start()].rstrip("(-_/+.,、，")
    return n


def nat_kind(v):
    """全国可发 → (展示原文, 语义 1可发/0不可发/2待确认/None)。"""
    if v is None:
        return None, None
    t = str(v).strip()
    if not t:
        return None, None
    t0 = t.replace(" ", "")
    if t0 in ("可以", "可", "可发", "是", "Y", "y", "1", "1.0", "√", "支持",
              "报备可发全国", "进货大于3年可以发外区"):
        return t, 1
    if t0 in ("不可以", "不可", "否", "N", "n", "0", "0.0", "不", "×", "x", "不支持",
              "进货小于3年不能发外区"):
        return ("不可以" if t0 in ("0", "0.0") else t), 0
    return t, 2


def sx(v):
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v) if float(v).is_integer() else float(v)
    t = str(v).strip().replace(",", "")
    if not t or t in ("-", "--", "—"):
        return None
    try:
        f = float(t)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    except ValueError:
        return t


# ------------------------------------------------------------------ xlsx 解析

def header_map(rows, need=("型号",), scan=4):
    for ri in range(min(scan, len(rows))):
        names = [sx(x) for x in (rows[ri] or [])]
        if not all(n in names for n in need):
            continue
        nxt = rows[ri + 1] if ri + 1 < len(rows) else []
        col = {}
        for i, n in enumerate(names):
            if n is None or re.fullmatch(r"\d+(\.\d+)?", str(n).strip()):
                v2 = sx(nxt[i]) if i < len(nxt) else None
                if v2:
                    n = v2
            if n:
                col.setdefault(n, i)
        return ri, col
    raise RuntimeError("找不到表头行 need=%s" % (need,))


def parse_clean(rows, kind):
    hi, col = header_map(rows, need=("型号", "品牌"))
    i_mo = col["型号"]
    i_q = col.get("滞销清理数量") if kind == "l" else col.get("样机清理数量")
    i_p = col.get("滞销清理价") if kind == "l" else col.get("样机清理价")
    i_rb = col.get("滞销出库奖励") if kind == "l" else (
        col.get("样机出库存奖励") or col.get("样机出库奖励"))
    i_av = col.get("可用量") if kind == "l" else col.get("现存量")

    def c(i, r):
        return r[i] if (i is not None and i < len(r)) else None

    out = []
    for r in rows[hi + 1:]:
        if not r:
            continue
        mo = sx(c(i_mo, r))
        if not mo or norm_model(mo) == "型号":
            continue
        if not sx(c(i_q, r)) and not sx(c(i_p, r)):
            continue
        nat_raw, nat_k = nat_kind(c(col.get("全国可发"), r))
        out.append({
            "kind": kind, "mo": mo,
            "b": sx(c(col.get("品牌"), r)), "cat": sx(c(col.get("品类"), r)),
            "code": sx(c(col.get("YS物料编码"), r)), "n": sx(c(col.get("YS物料名称"), r)),
            "lp": num(c(col.get("零售指导价"), r)), "nat": nat_raw, "natk": nat_k,
            "q": num(c(i_q, r)), "p": num(c(i_p, r)), "rb": num(c(i_rb, r)),
            "av": num(c(i_av, r)), "st": sx(c(col.get("产品状态"), r)),
        })
    return out


def parse_stock(rows):
    hi, col = header_map(rows, need=("型号", "可用量"))
    agg = {}

    def c(k, r):
        i = col.get(k)
        return r[i] if (i is not None and i < len(r)) else None

    for r in rows[hi + 1:]:
        if not r:
            continue
        mo = sx(c("型号", r))
        if not mo or norm_model(mo) == "型号":
            continue
        ck = core_of(mo)
        a = agg.setdefault(ck, {"mo": mo, "n": sx(c("物料名称", r)), "cat": sx(c("物料分类", r)),
                                "av": 0, "on": 0, "sp": 0, "gp": 0, "wh": {}})
        av, on = num(c("可用量", r)), num(c("现存量", r))
        sp, gp = num(c("样机现存量", r)), num(c("正品现存量", r))
        for k, v in (("av", av), ("on", on), ("sp", sp), ("gp", gp)):
            if isinstance(v, (int, float)):
                a[k] += v
        w = sx(c("仓库", r))
        if w:
            wt = sx(c("仓库类型", r))
            d = a["wh"].setdefault("%s|%s" % (w, wt or ""), {"w": w, "t": wt, "av": 0, "on": 0})
            if isinstance(av, (int, float)):
                d["av"] += av
            if isinstance(on, (int, float)):
                d["on"] += on
    return agg


def find_sheet(names, pred):
    for n in names:
        if pred(n):
            return n
    return None


def pick_sheet(names, *keys):
    for k in keys:
        for n in names:
            if k in n:
                return n
    return None


def parse_workbook(path, imgs_cache):
    """xlsx → payload（含 imgs）。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    names = wb.sheetnames

    def rows_of(name):
        return [list(r) for r in wb[name].iter_rows(values_only=True)]

    nl = pick_sheet(names, "滞销清理明细")
    ns = pick_sheet(names, "样机清理明细")
    n10 = find_sheet(names, lambda n: n.startswith("现存量1") or n.startswith("现存量2"))
    nst = find_sheet(names, lambda n: n == "现存量")

    recl = parse_clean(rows_of(nl), "l") if nl else []
    recs = parse_clean(rows_of(ns), "s") if ns else []

    inv_a = parse_stock(rows_of(n10)) if n10 else {}
    inv_b = parse_stock(rows_of(nst)) if nst else {}

    rows = {}
    for rec in recl + recs:
        ck = core_of(rec["mo"])
        r = rows.setdefault(ck, {"c": ck, "mo": rec["mo"], "b": rec["b"], "cat": rec["cat"],
                                 "code": rec["code"], "n": rec["n"], "lp": rec["lp"],
                                 "nat": rec["nat"], "natk": rec.get("natk"), "k": ""})
        for k in ("b", "cat", "code", "n", "lp"):
            if r.get(k) in (None, "") and rec.get(k) not in (None, ""):
                r[k] = rec[k]
        if rec["kind"] == "l":
            if "l" not in r["k"]:
                r["k"] += "l"
            r.update(lq=rec["q"], lpz=rec["p"], lb=rec["rb"], lav=rec["av"])
        else:
            if "s" not in r["k"]:
                r["k"] += "s"
            r.update(sq=rec["q"], spz=rec["p"], sb=rec["rb"], sav=rec["av"], st=rec["st"])
        if rec.get("mo") and len(str(rec["mo"])) > len(str(r.get("mo") or "")):
            r["mo"] = rec["mo"]
        if rec.get("nat") is not None:
            r["nat"], r["natk"] = rec["nat"], rec.get("natk")

    inv = dict(inv_b)
    inv.update(inv_a)
    for ck, r in rows.items():
        iv = inv.get(ck)
        if not iv:
            continue
        r["inv"] = {"av": iv["av"], "on": iv["on"], "sp": iv["sp"], "gp": iv["gp"],
                    "wh": sorted(iv["wh"].values(), key=lambda x: -x["av"])[:12]}
        if not r.get("n") and iv.get("n"):
            r["n"] = iv["n"]
        if not r.get("cat") and iv.get("cat"):
            r["cat"] = iv["cat"]

    allimg = imgs_cache or {}
    imgs = {ck: v for ck, v in allimg.items()
            if ck in rows and isinstance(v, str) and v.startswith("data:image/")}

    m = re.search(r"(\d{1,2})\s*月", nl or "")
    month = "%d-%02d" % (time.localtime().tm_year, int(m.group(1))) if m else ""

    return {
        "meta": {"month": month, "src": os.path.basename(path),
                 "built": time.strftime("%Y-%m-%d %H:%M"),
                 "online": {"share": ONLINE_SHARE, "fileId": FILE_ID}},
        "rows": sorted(rows.values(), key=lambda r: (r.get("b") or "", r.get("cat") or "", r["mo"])),
        "imgs": imgs,
    }


# ------------------------------------------------------------------ 渲染

def render(payload, template=None):
    if template is None:
        with io.open(rp("template.html"), encoding="utf-8") as f:
            template = f.read()
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raw = raw.replace("</", "<\\/")
    if "/*__DATA__*/" not in template:
        raise RuntimeError("模板缺少 /*__DATA__*/ 占位")
    return template.replace("/*__DATA__*/", raw, 1)


# ------------------------------------------------------------------ 状态

STATE = {"status": "idle", "msg": "", "built": "", "rev": 0}
SLOCK = threading.Lock()
LOG = []


def set_state(status=None, msg=None, rev=None, built=None):
    with SLOCK:
        if status is not None:
            STATE["status"] = status
        if msg is not None:
            STATE["msg"] = msg
        if rev is not None:
            STATE["rev"] = rev
        if built is not None:
            STATE["built"] = built


def read_state():
    with SLOCK:
        return dict(STATE)


# ------------------------------------------------------------------ 网络

def http_get(url, headers=None, timeout=180):
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, headers=hdrs)
    for ctx in (ssl.create_default_context(), ssl._create_unverified_context()):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except ssl.SSLError:
            continue
    raise RuntimeError("网络请求失败：" + url[:80])


def fetch_xlsx_by_url(url, log):
    code, raw = http_get(url, timeout=300)
    if raw[:2] == b"PK":
        log("已下载 %.2f MB" % (len(raw) / 1048576.0))
        return raw
    raise RuntimeError("直链返回的不是文件（HTTP %s）" % code)


# ------------------------------------------------------------------ 金山文档：走内嵌窗口

# 在「已经是 kdocs 域」的窗口里跑同步 XHR —— 同源，自动带上该窗口的登录态，
# 不用把 cookie 搬到 Python 这边来（陌生电脑扫码后一样能用）
SYNC_XHR = (
    "(function(){try{"
    "var x=new XMLHttpRequest();"
    "x.open('GET','/api/v3/office/file/" + FILE_ID + "/download',false);"
    "x.send();"
    "return x.status+'|'+String(x.responseText);"
    "}catch(e){return 'ERR|'+e.message;}})()"
)

MAIN_WIN = None
LOGIN_WIN = None
LOGIN_CLOSED = threading.Event()


def js_eval(win, script, default=None):
    if win is None:
        return default
    try:
        return win.evaluate_js(script)
    except Exception:
        return default


def kdocs_ready(win):
    return str(js_eval(win, "location.host", "") or "").endswith("kdocs.cn")


def direct_url_from_window(log):
    """返回 (直链, 错误)。错误 'NOTLOGIN' 表示窗口没登录。"""
    if LOGIN_WIN is None or LOGIN_CLOSED.is_set():
        return None, "登录窗口不可用"
    if not kdocs_ready(LOGIN_WIN):
        return None, "NOTLOGIN"
    r = js_eval(LOGIN_WIN, SYNC_XHR)
    if r is None:
        return None, "执行脚本失败"
    r = str(r)
    if r.startswith("ERR|"):
        return None, r[4:]
    _, _, txt = r.partition("|")
    if txt[:2] == "PK":
        return None, "端点直接返回了文件"
    try:
        d = json.loads(txt)
    except Exception:
        return None, "返回非 JSON：" + txt[:160]
    if str(d.get("result")).lower() in ("usernotlogin", "notlogin"):
        return None, "NOTLOGIN"
    du = d.get("download_url") or d.get("url") or ""
    if isinstance(du, str) and du.startswith("http"):
        return du, None
    return None, "拿不到直链：" + txt[:160]


def open_login_window(log):
    """把登录窗口导航到在线表所在域，等页面就绪。"""
    set_state("running", "正在打开金山文档…")
    log("打开金山文档页面…")
    LOGIN_WIN.load_url(ONLINE_SHARE)
    for _ in range(40):
        time.sleep(0.5)
        if LOGIN_CLOSED.is_set():
            return False
        if kdocs_ready(LOGIN_WIN) and js_eval(LOGIN_WIN, "document.readyState") == "complete":
            time.sleep(1.0)
            return True
    return kdocs_ready(LOGIN_WIN)


def obtain_download_url(log, allow_prompt=True, wait_s=900):
    """拿到 xlsx 的签名直链。已登录会静默完成；未登录则弹窗等人扫码。"""
    if LOGIN_WIN is None or LOGIN_CLOSED.is_set():
        raise RuntimeError("登录窗口不可用，无法从在线表更新")

    if not kdocs_ready(LOGIN_WIN):
        open_login_window(log)

    # 先静默重试：在线表是公开分享的，页面加载后十几秒匿名会话往往就能拿到直链
    # —— 别一上来就弹扫码窗吓人，绝大部分人根本不用登录
    set_state("running", "正在连接在线表…（如果需要登录，会自动弹出扫码窗口）")
    for i in range(8):
        du, err = direct_url_from_window(log)
        if du:
            log("静默取到直链（用时约 %d 秒）" % (i * 2))
            return du
        if i == 0 and err != "NOTLOGIN":
            log("直链异常：%s（继续重试）" % err)
        time.sleep(2.5)

    if not allow_prompt:
        raise RuntimeError("拿不到下载直链（在线表可能需要登录）")

    log("静默期没拿到：弹出登录窗口等人扫码")
    try:
        LOGIN_WIN.show()
    except Exception:
        pass
    set_state("login", "请在弹出的窗口里用微信/手机扫码登录金山文档，登录后会自动继续（无需点任何按钮）")

    t0 = time.time()
    tick = 0
    while time.time() - t0 < wait_s:
        time.sleep(3)
        if LOGIN_CLOSED.is_set():
            raise RuntimeError("登录窗口被关闭，已取消更新")
        du, err = direct_url_from_window(log)
        if du:
            log("登录成功，取到直链")
            try:
                LOGIN_WIN.hide()
            except Exception:
                pass
            return du
        tick += 1
        if tick % 4 == 0:
            left = int((wait_s - (time.time() - t0)) / 60)
            set_state("login", "等待扫码登录中…（还剩约 %d 分钟；登录成功后自动继续）" % max(left, 1))
    try:
        LOGIN_WIN.hide()
    except Exception:
        pass
    raise RuntimeError("等待登录超时，已取消更新")


def pick_xlsx(log, log_dir_scan=True):
    """在线表若有本地最新 xlsx 也不必扫盘 —— 直接返回 None（走网络）。"""
    return None


# ------------------------------------------------------------------ 更新流程

_update_lock = threading.Lock()


def load_current_payload():
    """当前数据：优先工作目录里的（更新后落在那），否则用 exe 内嵌的。"""
    p = wp("payload.json")
    if not os.path.isfile(p):
        p = rp("payload.json")
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def rebuild_from_xlsx(path, log):
    old = load_current_payload()
    cache = old.get("imgs") or {}
    # 全量图库（assets/images_cache.json）优先，老 payload 的图兜底
    for p in (rp("images_cache.json"),):
        if os.path.isfile(p):
            try:
                with io.open(p, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception:
                pass
    payload = parse_workbook(path, cache)
    for ck, v in (old.get("imgs") or {}).items():
        payload["imgs"].setdefault(ck, v)
    html = render(payload)
    with io.open(wp("index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    with io.open(wp("payload.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)      # 记为「当前数据」，下次启动沿用
    log("页面已重建：%d 个型号 / %d 张图" % (len(payload["rows"]), len(payload["imgs"])))
    set_state(built=payload["meta"]["built"])
    return payload


def do_update(log, xlsx_bytes=None, xlsx_path=None):
    if not _update_lock.acquire(blocking=False):
        return False, "已有更新在进行中"
    try:
        if xlsx_bytes is not None:
            set_state("running", "正在解析上传的 Excel…")
            path = wp("data.xlsx")
            with open(path, "wb") as f:
                f.write(xlsx_bytes)
            log("使用本地文件（%.2f MB）" % (len(xlsx_bytes) / 1048576.0))
        elif xlsx_path:
            set_state("running", "正在解析本地 Excel…")
            path = xlsx_path
            log("使用本地文件：%s" % xlsx_path)
        else:
            set_state("running", "正在连接金山文档…")
            du = obtain_download_url(log)
            set_state("running", "正在下载在线表…")
            raw = fetch_xlsx_by_url(du, log)
            path = wp("data.xlsx")
            with open(path, "wb") as f:
                f.write(raw)

        set_state("running", "正在解析并重建页面…")
        rebuild_from_xlsx(path, log)
        set_state("done", "更新完成")
        return True, "更新完成"
    except Exception as e:
        msg = str(e) or repr(e)
        log("更新失败：%s" % msg)
        log(traceback.format_exc())
        set_state("error", msg)
        return False, msg
    finally:
        _update_lock.release()


# ------------------------------------------------------------------ HTTP（本地服务：给窗口里的页面用）

class Handler(BaseHTTPRequestHandler):
    server_version = "ModelLookup/2.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path

        if path in ("/", "/index.html"):
            p = wp("index.html")
            if not os.path.isfile(p):
                self._send(503, "页面还没生成", "text/plain; charset=utf-8")
                return
            with io.open(p, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        if path == "/api/ping":
            self._send(200, json.dumps({"app": APP_ID, "ver": APP_VERSION}, ensure_ascii=False))
            return

        if path == "/api/focus":
            try:
                MAIN_WIN.restore()
                MAIN_WIN.show()
            except Exception:
                pass
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return

        if path == "/api/state":
            s = read_state()
            if not s.get("built"):
                try:
                    s["built"] = json.load(io.open(rp("payload.json"), encoding="utf-8"))["meta"]["built"]
                except Exception:
                    pass
            self._send(200, json.dumps(s, ensure_ascii=False))
            return

        if path == "/api/update":
            if read_state()["status"] in ("running", "login"):
                self._send(200, json.dumps({"ok": False, "msg": "正在更新中，请稍候"},
                                           ensure_ascii=False))
                return
            set_state("running", "正在连接金山文档…")
            threading.Thread(target=do_update, args=(LOG.append,), daemon=True).start()
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return

        if path == "/api/cancel":
            set_state("error", "已取消")
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return

        self._send(404, json.dumps({"ok": False, "msg": "not found"}, ensure_ascii=False))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path != "/api/upload":
            self._send(404, json.dumps({"ok": False, "msg": "not found"}, ensure_ascii=False))
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            # 页面用 base64 传（避免 multipart 解析）
            txt = raw.decode("utf-8", "ignore").strip()
            if txt.startswith("data:"):
                txt = txt.split(",", 1)[-1]
            data = base64.b64decode(txt)
            if data[:2] != b"PK":
                raise RuntimeError("这不是一个 xlsx 文件")
            if read_state()["status"] in ("running", "login"):
                self._send(200, json.dumps({"ok": False, "msg": "正在更新中，请稍候"},
                                           ensure_ascii=False))
                return
            set_state("running", "正在解析上传的 Excel…")
            threading.Thread(target=do_update, args=(LOG.append,), kwargs={"xlsx_bytes": data},
                             daemon=True).start()
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))


# 固定端口：localStorage 是按 origin 隔离的，端口一变用户标的「已发布」「我的定价」就全丢
PORTS = [17321, 17322, 17323, 17324, 17325, 17326, 17327, 17328]
HOST = "127.0.0.1"


def port_alive(port, timeout=0.6):
    """返回 True 表示本程序已经在跑（而不是别的程序占着端口）。"""
    try:
        with urllib.request.urlopen("http://%s:%d/api/ping" % (HOST, port), timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d.get("app") == APP_ID
    except Exception:
        return False


def activate_existing(port):
    try:
        urllib.request.urlopen("http://%s:%d/api/focus" % (HOST, port), timeout=1.5).read()
        return True
    except Exception:
        return False


def start_server():
    httpd = None
    port = 0
    for p in PORTS:
        try:
            httpd = ThreadingHTTPServer((HOST, p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:                       # 全被别的东西占了才退回随机端口
        s = socket.socket()
        s.bind((HOST, 0))
        port = s.getsockname()[1]
        s.close()
        httpd = ThreadingHTTPServer((HOST, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ------------------------------------------------------------------ 窗口 / 应用

def _dpi():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def work_area():
    """主显示器可用区域 (x, y, w, h)。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        r = wt.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            w, h = r.right - r.left, r.bottom - r.top
            if w > 200 and h > 200:
                return r.left, r.top, w, h
    except Exception:
        pass
    return 0, 0, 1280, 800


def fit_window(want_w, want_h):
    """窗口别比屏幕还大（远程桌面 / 小屏笔记本上很常见）。"""
    x, y, W, H = work_area()
    w = min(want_w, max(900, int(W * 0.94)))
    h = min(want_h, max(620, int(H * 0.94)))
    return w, h, x + (W - w) // 2, y + (H - h) // 2


def _webview2_pv():
    """已安装的 WebView2 运行时版本（没有就返回 None）。"""
    try:
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
                                       r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
                                       r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
                                      r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ]
        for root, sub in keys:
            try:
                with winreg.OpenKey(root, sub) as k:
                    v = winreg.QueryValueEx(k, "pv")[0]
                if v and v != "0.0.0.0":
                    return v
            except OSError:
                continue
    except Exception:
        pass
    return None


def build_menu():
    from webview.menu import Menu, MenuAction, MenuSeparator

    def upd():
        threading.Thread(target=do_update, args=(LOG.append,), daemon=True).start()

    def about():
        webview.windows[0].evaluate_js(
            "alert('型号查询 v%s\\n\\n隆阳电器 · 滞销/样机清理明细（公布版）\\n"
            "数据来源：金山文档在线表\\n内置 %s')" % (APP_VERSION, _built_desc())
        )

    def source():
        try:
            import webbrowser
            webbrowser.open(ONLINE_SHARE)
        except Exception:
            pass

    return [
        Menu("数据(&D)", [
            MenuAction("从金山文档更新", upd),
            MenuAction("打开在线表（浏览器）", source),
        ]),
        Menu("帮助(&H)", [
            MenuAction("关于", about),
        ]),
    ]


def _built_desc():
    try:
        p = load_current_payload()
        return "%d 个型号 / %d 张机图 · 数据 %s" % (
            len(p["rows"]), len(p.get("imgs") or {}), p["meta"].get("built"))
    except Exception:
        return "数据未就绪"


def warn_no_webview2():
    """没有 WebView2 运行时 → 用原生 MessageBox 说清楚，别让用户干瞪眼。"""
    msg = ("本机缺少 Microsoft Edge WebView2 运行时，程序没法显示界面。\n\n"
           "装一下就好（免费，2MB）：\n"
           "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
           "装完重新打开本程序即可。")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
    except Exception:
        pass


def run_app(httpd, port, startup_xlsx=None):
    global MAIN_WIN, LOGIN_WIN
    url = "http://%s:%d/" % (HOST, port)

    _dpi()                                  # 必须在量屏幕 / 建窗前：否则算出来是缩放前的尺寸
    w, h, x, y = fit_window(1480, 940)
    lw, lh, lx, ly = fit_window(1180, 860)

    MAIN_WIN = webview.create_window(
        "%s · 隆阳电器（公布版）" % APP_NAME,
        url=url,
        width=w, height=h, x=x, y=y,
        min_size=(900, 600),
        background_color="#f5f7fb",
        text_select=True,
        menu=build_menu(),
    )
    # 登录窗口：先建好藏着，需要扫码时才 show()（创建必须发生在 start() 之前）
    LOGIN_WIN = webview.create_window(
        "登录金山文档 · 扫码后不用点任何按钮",
        html="<body style='background:#fff;font:14px \"Microsoft YaHei\";padding:30px;color:#5b6577'>"
             "正在打开金山文档…</body>",
        width=lw, height=lh, x=lx, y=ly, min_size=(700, 520), hidden=True,
    )

    def on_login_closed():
        LOGIN_CLOSED.set()

    try:
        LOGIN_WIN.events.closed += on_login_closed
    except Exception:
        pass

    def on_main_closed():
        set_state("error", "窗口已关闭")
        try:
            if LOGIN_WIN is not None and not LOGIN_CLOSED.is_set():
                LOGIN_WIN.destroy()
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass

    try:
        MAIN_WIN.events.closed += on_main_closed
    except Exception:
        pass

    def after_start():
        LOG.append(lambda m: print("[%s] %s" % (APP_NAME, m)))
        LOG.append(lambda m: None)
        if startup_xlsx:
            time.sleep(1.0)
            do_update(LOG.append, xlsx_path=startup_xlsx)

    webview.start(after_start, gui="edgechromium", debug=False,
                  private_mode=False, storage_path=wp("webview"),
                  http_server=False)


def main():
    if not webview2_ok():
        warn_no_webview2()
        return

    # 已经在跑 → 把那个窗口叫到前面来，别开第二个（否则端口变了，本地标记会丢）
    for p in PORTS:
        if port_alive(p):
            if activate_existing(p):
                return

    arg = None
    for a in sys.argv[1:]:
        if a.lower().endswith((".xlsx", ".xls")) and os.path.isfile(a):
            arg = a
            break

    bootstrap()
    httpd, port = start_server()
    run_app(httpd, port, arg)


def webview2_ok():
    return _webview2_pv() is not None


def bootstrap():
    """用当前数据渲染 index.html —— 每次启动都重建，保证页面与数据永远一致。"""
    payload = load_current_payload()
    with io.open(wp("index.html"), "w", encoding="utf-8") as f:
        f.write(render(payload))
    return wp("index.html")


# ------------------------------------------------------------------ 自检

def _check():
    out = []
    out.append("%s v%s 自检" % (APP_NAME, APP_VERSION))
    out.append("frozen = %s" % is_frozen())
    out.append("exe = %s" % sys.executable)
    out.append("资源目录 = %s" % res_dir())
    out.append("工作目录 = %s" % work_dir())
    out.append("WebView2 运行时 = %s %s" % (_webview2_pv() or "无", "✅" if webview2_ok() else "（目标机需要）"))
    # 注意：构建机上没有 WebView2 不算打包问题 —— 那是「运行环境」的事，
    # 程序启动时会用原生弹窗提示用户装。所以这里只警告，不判失败。
    ok = True

    try:
        out.append("pywebview = %s ✅" % (getattr(webview, "__version__", "?"),))
    except Exception as e:
        ok = False
        out.append("pywebview ❌ %r" % (e,))

    # ⚠ 这条必须留着：winforms 顶层硬依赖 webview.platforms.win32，
    #   打包时把它 excludes 掉，pywebview 会误导性地报「You must have pythonnet installed」
    try:
        import webview.platforms.winforms as _wf   # noqa: F401
        out.append("winforms 后端 = 可导入 ✅")
    except Exception as e:
        ok = False
        out.append("winforms 后端 ❌ 界面根本起不来！%r" % (e,))

    for mod in ("clr", "pythonnet"):
        try:
            __import__(mod)
            out.append("%s ✅" % mod)
        except Exception as e:
            ok = False
            out.append("%s ❌ %r" % (mod, e))

    try:
        out.append("openpyxl = %s ✅" % openpyxl.__version__)
    except Exception as e:
        ok = False
        out.append("openpyxl ❌ 缺了会导致没法解析表格！%r" % (e,))

    for name in ("template.html", "payload.json", "images_cache.json"):
        p = rp(name)
        if os.path.isfile(p):
            out.append("%s = %.2f MB ✅" % (name, os.path.getsize(p) / 1048576.0))
        else:
            ok = False
            out.append("%s ❌ 打包漏带！" % name)

    if is_frozen() and os.path.isfile(rp("cookies.json")):
        ok = False
        out.append("❌ 打包里带了 cookies.json（本机登录态），不应该外发！")

    try:
        with io.open(rp("payload.json"), encoding="utf-8") as f:
            p = json.load(f)
        n_img = len(p.get("imgs") or {})
        img_ok = all(isinstance(v, str) and v.startswith("data:image/")
                     for v in (p.get("imgs") or {}).values())
        out.append("payload: 型号 %d 个 / 图 %d 张 / dataURL 格式 %s ✅"
                   % (len(p["rows"]), n_img, "正常" if img_ok else "异常"))
        if n_img == 0:
            ok = False
            out.append("payload 里一张图都没有 ❌")
        html = render(p)
        out.append("渲染试跑：HTML %.2f MB ✅" % (len(html.encode("utf-8")) / 1048576.0))
    except Exception as e:
        ok = False
        out.append("渲染试跑 ❌ %r" % (e,))

    try:
        httpd, port = start_server()
        with urllib.request.urlopen("http://%s:%d/api/ping" % (HOST, port), timeout=6) as r:
            d = json.loads(r.read().decode("utf-8"))
        out.append("本地服务 = 端口 %d / app=%s ✅" % (port, d.get("app")))
        httpd.shutdown()
    except Exception as e:
        ok = False
        out.append("本地服务 ❌ %r" % (e,))

    try:
        bootstrap()
        out.append("查询页 = %s（%.2f MB）✅" % (
            wp("index.html"), os.path.getsize(wp("index.html")) / 1048576.0))
    except Exception as e:
        ok = False
        out.append("生成查询页 ❌ %r" % (e,))

    if is_frozen():
        mb = os.path.getsize(sys.executable) / 1048576.0
        out.append("exe 体积 = %.2f MB" % mb)
        if mb > 90:
            out.append("⚠ 体积偏大，检查是不是把 numpy/PIL 之类拖进来了")

    out.append("判定：自检%s" % ("通过 ✅" if ok else "失败 ❌"))
    txt = "\n".join(out)
    logdir = os.path.dirname(sys.executable) if is_frozen() else os.path.dirname(os.path.abspath(__file__))
    try:
        with io.open(os.path.join(logdir, "exe_check.log"), "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass
    try:
        sys.stdout.write(txt + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return txt


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    else:
        try:
            main()
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, traceback.format_exc(),
                                                 APP_NAME + " 启动失败", 0x10)
            except Exception:
                pass
