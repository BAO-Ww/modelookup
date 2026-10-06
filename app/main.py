# -*- coding: utf-8 -*-
"""
型号查询 · 隆阳电器
=====================
单文件桌面工具：
  · 双击 → 起本地服务 + 用默认浏览器打开离线查询页（数据已内嵌，秒开）
  · 页面上「从金山文档更新数据」→ 用保存的登录态拉最新在线表 → 重新生成页面 → 自动刷新
  · 也支持把从金山文档下载的 xlsx 直接拖到 exe 图标上重建

自检: 型号查询.exe --check     （结果写到 exe 同目录 exe_check.log）
"""
import base64
import io
import json
import os
import queue
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import webbrowser
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 顶层 import（不能放进函数里）：放函数内会被 PyInstaller 判定为「延迟/可选」而不打包
import openpyxl  # noqa: E402

APP_NAME = "型号查询"
APP_VERSION = "1.0"

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
    d = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "ModelLookup")
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


# ------------------------------------------------------------------ 金山文档

def load_cookies():
    for p in (wp("cookies.json"), rp("cookies.json")):
        if os.path.isfile(p):
            try:
                d = json.load(io.open(p, encoding="utf-8-sig"))
                cs = d.get("result", {}).get("cookies", [])
                if cs:
                    return "; ".join("%s=%s" % (c["name"], c["value"]) for c in cs), p
            except Exception:
                continue
    return None, None


def kdocs_download(ck, log):
    """下载在线表 → 返回 xlsx 字节。"""
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://www.kdocs.cn/l/" + FILE_SHARE, "Cookie": ck,
            "Origin": "https://www.kdocs.cn"}

    def get(url, timeout=120):
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r, r.read()

    # 0) 登录态体检
    try:
        r, raw = get("https://www.kdocs.cn/api/v3/office/file/" + FILE_SHARE)
        meta = json.loads(raw.decode("utf-8", "ignore"))
        if str(meta.get("result")) in ("userNotLogin", "fail") or meta.get("code") in (40000, 401):
            raise RuntimeError("登录态已过期")
        f = (meta.get("file") or {})
        log("在线表：%s（%s，改于 %s）" % (
            f.get("name"), f.get("size"),
            time.strftime("%Y-%m-%d %H:%M", time.localtime(f.get("modify_time", 0) or 0))))
    except RuntimeError:
        raise
    except Exception as e:
        log("元信息读取失败（继续尝试下载）：%r" % (e,))

    # 1) 下载
    for u in ("https://www.kdocs.cn/api/v3/office/file/%s/download" % FILE_ID,
              "https://www.kdocs.cn/api/v3/office/file/%s/download" % FILE_SHARE):
        try:
            r, raw = get(u)
            if raw[:2] == b"PK":
                log("已下载 %d 字节" % len(raw))
                return raw
            txt = raw[:600].decode("utf-8", "ignore")
            m = re.search(r'https?://[^"\'\s]+', txt)
            if m:
                u2 = m.group(0).replace("\\u0026", "&").replace("\\/", "/")
                r2, raw2 = get(u2)
                if raw2[:2] == b"PK":
                    log("已下载 %d 字节（直链）" % len(raw2))
                    return raw2
            low = txt.lower()
            if "notlogin" in low or "登录" in txt:
                raise RuntimeError("登录态已过期，请重新登录金山文档")
            log("端点返回非文件：%s" % txt[:150].replace("\n", " "))
        except RuntimeError:
            raise
        except urllib.error.HTTPError as e:
            log("下载端点 HTTP %s" % e.code)
        except Exception as e:
            log("下载端点失败：%r" % (e,))
    raise RuntimeError("下载失败：拿不到 xlsx（登录态可能已过期）")


# ------------------------------------------------------------------ 状态

STATE = {"status": "idle", "msg": "", "built": "", "rev": 0}
SLOCK = threading.Lock()


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


# ------------------------------------------------------------------ 更新流程

_update_lock = threading.Lock()


def rebuild_from_xlsx(path, log):
    with io.open(rp("payload.json"), encoding="utf-8") as f:
        old = json.load(f)
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
    out = wp("index.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    log("页面已重建：%d 个型号 / %d 张图" % (len(payload["rows"]), len(payload["imgs"])))
    set_state(built=payload["meta"]["built"])
    return payload


def do_update(log, xlsx_path=None):
    if not _update_lock.acquire(blocking=False):
        return False, "已有更新在进行中"
    try:
        if xlsx_path is None:
            set_state("running", "正在读取登录态…")
            ck, src = load_cookies()
            if not ck:
                set_state("error", "没有找到登录态，请在下方用「本地 Excel」方式更新")
                return False, "没有登录态"
            log("登录态来自 %s" % src)
            set_state("running", "正在从金山文档下载…")
            raw = kdocs_download(ck, log)
            set_state("running", "正在写入临时文件…")
            xlsx_path = wp("data.xlsx")
            with open(xlsx_path, "wb") as f:
                f.write(raw)
        else:
            set_state("running", "正在解析本地 Excel…")
            log("使用本地文件：%s" % xlsx_path)

        set_state("running", "正在解析并重建页面…")
        rebuild_from_xlsx(xlsx_path, log)
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


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "ModelLookup/1.0"

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
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            p = wp("index.html")
            if not os.path.isfile(p):
                self._send(503, "页面还没生成", "text/plain; charset=utf-8")
                return
            with io.open(p, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            s = read_state()
            try:
                s["built"] = s.get("built") or json.load(
                    io.open(rp("payload.json"), encoding="utf-8"))["meta"]["built"]
            except Exception:
                pass
            self._send(200, json.dumps(s, ensure_ascii=False))
            return
        if path == "/api/update":
            if read_state()["status"] == "running":
                self._send(200, json.dumps({"ok": False, "msg": "正在更新中，请稍候"},
                                           ensure_ascii=False))
                return
            set_state("running", "正在连接金山文档…")
            threading.Thread(target=do_update, args=(LOG.append,), daemon=True).start()
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        self._send(404, json.dumps({"ok": False, "msg": "not found"}, ensure_ascii=False))


LOG = []


def start_server():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


# ------------------------------------------------------------------ GUI

def _dpi():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


INK, SUB, LINE = "#1b2431", "#5b6577", "#e6eaf2"
BRAND, BRAND_D, BRAND_L = "#2e6be6", "#1b47b8", "#eaf1ff"
FONT = "Microsoft YaHei UI"


def run_gui(httpd, port, root=None):
    import tkinter as tk
    from tkinter import ttk, filedialog

    own = root is None
    if own:
        _dpi()
        root = tk.Tk()
    root.title("%s v%s · 隆阳电器" % (APP_NAME, APP_VERSION))
    root.configure(bg="#f4f6fa")
    root.minsize(560, 420)
    W, H = 620, 470
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("%dx%d+%d+%d" % (W, H, max(0, (sw - W) // 2), max(0, (sh - H) // 3)))

    st = ttk.Style()
    try:
        st.theme_use("clam")
    except Exception:
        pass
    st.configure(".", font=(FONT, 10), background="#f4f6fa", foreground=INK)
    st.configure("Panel.TFrame", background="#f4f6fa")
    st.configure("Card.TFrame", background="#ffffff")
    st.configure("H.TLabel", font=(FONT, 13, True), background="#f4f6fa", foreground=INK)
    st.configure("Sub.TLabel", font=(FONT, 9), background="#f4f6fa", foreground=SUB)
    st.configure("CardSub.TLabel", font=(FONT, 9), background="#ffffff", foreground=SUB)
    st.configure("Ok.TLabel", font=(FONT, 10, True), background="#ffffff", foreground="#0f7d45")
    st.configure("Err.TLabel", font=(FONT, 10, True), background="#ffffff", foreground="#d64545")
    st.configure("Primary.TButton", font=(FONT, 10, True), foreground="#ffffff",
                 background=BRAND, borderwidth=0, padding=(14, 9))
    st.map("Primary.TButton", background=[("active", BRAND_D), ("disabled", "#a9c0ee")])
    st.configure("Soft.TButton", font=(FONT, 10), foreground=BRAND_D,
                 background=BRAND_L, borderwidth=0, padding=(12, 8))
    st.map("Soft.TButton", background=[("active", "#dbe7ff")])

    top = ttk.Frame(root, style="Panel.TFrame")
    top.pack(fill="x", padx=18, pady=(16, 8))
    ttk.Label(top, text="型号查询", style="H.TLabel").pack(anchor="w")
    ttk.Label(top, text="隆阳电器 · 滞销 / 样机清理明细（公布版）", style="Sub.TLabel").pack(anchor="w", pady=(2, 0))

    card = ttk.Frame(root, style="Card.TFrame")
    card.pack(fill="x", padx=18, pady=(4, 10))
    inner = tk.Frame(card, bg="#ffffff")
    inner.pack(fill="x", padx=14, pady=12)

    lbl_state = tk.Label(inner, text="就绪", bg="#ffffff", fg="#0f7d45",
                         font=(FONT, 10, "bold"), anchor="w")
    lbl_state.pack(fill="x")
    lbl_msg = tk.Label(inner, text="", bg="#ffffff", fg=SUB, font=(FONT, 9),
                       anchor="w", justify="left", wraplength=520)
    lbl_msg.pack(fill="x", pady=(3, 0))

    btns = tk.Frame(root, bg="#f4f6fa")
    btns.pack(fill="x", padx=18)
    b_open = ttk.Button(btns, text="打开查询页", style="Primary.TButton",
                        command=lambda: webbrowser.open("http://127.0.0.1:%d/" % port))
    b_upd = ttk.Button(btns, text="从金山文档更新", style="Soft.TButton", command=lambda: on_update(None))
    b_loc = ttk.Button(btns, text="用本地 Excel 更新", style="Soft.TButton", command=lambda: on_local())
    for b in (b_open, b_upd, b_loc):
        b.pack(side="left", padx=(0, 8))

    tk.Label(root, text="运行日志", bg="#f4f6fa", fg=SUB, font=(FONT, 9),
             anchor="w").pack(fill="x", padx=18, pady=(14, 4))
    box = tk.Frame(root, bg="#ffffff", highlightthickness=1, highlightbackground=LINE)
    box.pack(fill="both", expand=True, padx=18, pady=(0, 16))
    txt = tk.Text(box, bg="#ffffff", fg="#33404f", relief="flat", wrap="word",
                  font=("Consolas", 9), padx=10, pady=8, height=10)
    txt.pack(fill="both", expand=True)
    txt.configure(state="disabled")
    sb = ttk.Scrollbar(box, command=txt.yview)
    txt.configure(yscrollcommand=sb.set)

    Q = queue.Queue()
    LOG.append(lambda m: Q.put(("log", str(m))))
    STOP = {"v": False}

    def append_line(s):
        txt.configure(state="normal")
        txt.insert("end", time.strftime("[%H:%M] ") + s.rstrip() + "\n")
        txt.see("end")
        txt.configure(state="disabled")

    def set_busy(busy):
        for b in (b_upd, b_loc):
            b.configure(state="disabled" if busy else "normal")

    def show(state, msg):
        col = {"ok": "#0f7d45", "err": "#d64545"}.get(state, BRAND_D)
        lbl_state.configure(text={"ok": "✔ 更新完成", "err": "✘ 更新失败"}.get(state, "● 正在更新…"),
                            fg=col)
        lbl_msg.configure(text=msg or "")

    last = {"s": "idle"}

    def pump():
        try:
            while True:
                kind, payload = Q.get_nowait()
                if kind == "log":
                    append_line(payload)
                elif kind == "result":
                    ok, msg = payload
                    set_busy(False)
                    show("ok" if ok else "err", msg)
                    if ok:
                        webbrowser.open("http://127.0.0.1:%d/" % port)
        except queue.Empty:
            pass

        if not STOP["v"]:
            s = read_state()
            cur = s.get("status")
            if cur == "running":
                set_busy(True)
                show("run", s.get("msg") or "")
            elif cur != last["s"]:              # 只在状态跳变时刷一次，避免和页面抢状态
                if cur == "done":
                    show("ok", "数据已更新到最新")
                    set_busy(False)
                elif cur == "error":
                    show("err", s.get("msg") or "")
                    set_busy(False)
            last["s"] = cur
        if STOP["v"]:
            return
        root.after(350, pump)

    def work(xlsx):
        if not xlsx:
            Q.put(("log", "开始从金山文档更新…"))
        else:
            Q.put(("log", "使用本地文件：%s" % xlsx))
        ok, msg = do_update(lambda m: Q.put(("log", str(m))), xlsx)
        Q.put(("result", (ok, msg)))

    def on_update(xlsx):
        if read_state()["status"] == "running":
            return
        set_busy(True)
        show("run", "正在启动更新…")
        set_state("running", "正在连接金山文档…")
        threading.Thread(target=work, args=(xlsx,), daemon=True).start()

    def on_local():
        p = filedialog.askopenfilename(title="选择从金山文档下载的 Excel",
                                       filetypes=[("Excel 工作簿", "*.xlsx *.xls"), ("所有文件", "*.*")])
        if p:
            on_update(p)

    def on_close():
        STOP["v"] = True
        try:
            httpd.shutdown()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

    root.protocol("WM_DELETE_WINDOW", on_close)
    append_line("本机服务已启动：http://127.0.0.1:%d/" % port)
    append_line("页面里点「从金山文档更新数据」也可直接更新。")
    pump()
    if own:
        root.mainloop()
    return root


# ------------------------------------------------------------------ 启动

def bootstrap():
    """确保 index.html 存在（首次从内嵌 payload 渲染）。"""
    out = wp("index.html")
    if not os.path.isfile(out):
        with io.open(rp("payload.json"), encoding="utf-8") as f:
            payload = json.load(f)
        with io.open(out, "w", encoding="utf-8") as f:
            f.write(render(payload))
    return out


def main():
    arg = None
    for a in sys.argv[1:]:
        if a.lower().endswith((".xlsx", ".xls")) and os.path.isfile(a):
            arg = a
            break
    bootstrap()
    httpd, port = start_server()
    if arg:
        def _bg():
            do_update(LOG.append, arg)
            webbrowser.open("http://127.0.0.1:%d/" % port)
        threading.Thread(target=_bg, daemon=True).start()
    else:
        webbrowser.open("http://127.0.0.1:%d/" % port)
    run_gui(httpd, port)


# ------------------------------------------------------------------ 自检

def _check():
    out = []
    out.append("%s v%s 自检" % (APP_NAME, APP_VERSION))
    out.append("frozen = %s" % is_frozen())
    out.append("exe = %s" % sys.executable)
    out.append("资源目录 = %s" % res_dir())
    out.append("工作目录 = %s" % work_dir())
    ok = True

    try:
        import tkinter
        r = tkinter.Tk()
        r.destroy()
        out.append("tkinter = %s ✅" % tkinter.TkVersion)
    except Exception as e:
        ok = False
        out.append("tkinter ❌ %r" % (e,))

    try:
        import openpyxl
        out.append("openpyxl = %s ✅" % openpyxl.__version__)
    except Exception as e:
        ok = False
        out.append("openpyxl ❌ 缺了会导致没法解析表格！%r" % (e,))

    for name in ("template.html", "payload.json", "cookies.json"):
        p = rp(name)
        if os.path.isfile(p):
            out.append("%s = %.1f KB ✅" % (name, os.path.getsize(p) / 1024.0))
        else:
            ok = False
            out.append("%s ❌ 打包漏带！" % name)

    try:
        with io.open(rp("payload.json"), encoding="utf-8") as f:
            p = json.load(f)
        n_img = len(p.get("imgs") or {})
        img_ok = all(isinstance(v, str) and v.startswith("data:image/jpeg;base64,")
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

    ck, src = load_cookies()
    out.append("登录态：%s（%s）%s" % ("有" if ck else "无", src or "-", "✅" if ck else "⚠"))

    try:
        httpd, port = start_server()
        req = urllib.request.Request("http://127.0.0.1:%d/api/state" % port)
        with urllib.request.urlopen(req, timeout=6) as r:
            s = json.loads(r.read().decode("utf-8"))
        out.append("本地服务 = 端口 %d / state=%s ✅" % (port, s.get("status")))
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
        if mb > 45:
            out.append("⚠ 体积偏大，可能又有大依赖被拖进来了")

    out.append("判定：自检%s" % ("通过 ✅" if ok else "失败 ❌"))
    txt = "\n".join(out)
    with io.open(os.path.join(os.path.dirname(sys.executable), "exe_check.log"),
                 "w", encoding="utf-8") as f:
        f.write(txt)
    return txt


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    else:
        try:
            main()
        except Exception:
            import tkinter.messagebox as mb
            mb.showerror(APP_NAME, "启动失败：\n\n" + traceback.format_exc())
