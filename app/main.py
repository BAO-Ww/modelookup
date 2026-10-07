# -*- coding: utf-8 -*-
"""
型号查询 · 隆阳电器（公布版）  v2.4
====================================
独立窗口的桌面软件（自带 WebView2 内核，无地址栏 / 无浏览器外壳）：

  · 双击 → 先亮骨架页（页面只有几十 KB），数据随后从本地服务取 → 窗口约 1.5 秒出现
  · 页面上 / 菜单里「从金山文档更新」→ 自动拉两个在线表 → 重建 → 自动刷新
      · 已登录：后台静默完成，窗口都不闪
      · 未登录：弹出「登录金山文档」窗口，扫码后自动继续
  · 「更新实时库存」→ 从用友 YonSuite 直接拉现存量（实时数据，非每月导表）
      · 登录一次存进本机，之后点更新直接拉，不用再登录
      · 未登录：弹出「用友登录」窗口，登录后自动继续
  · 也支持把从金山文档下载的 xlsx 拖到 exe 图标上重建

数据源（两个）：
  ① 公布版-滞销样机清理明细   分享码 ch7gwGTP9De4 / fileId 548020345856
        只读「10月滞销清理明细」「10月样机清理明细」两个 sheet（用户明确要求，别的不读）
  ② 2026年10月价格表-LYDF-共享  分享码 cficbK9Drrdp（fileId 运行时探测后缓存）
        只读 sheet「型号价格表（只看这个）」

自检:  型号查询.exe --check        （结果写到 exe 同目录 exe_check.log）
"""
import base64
import datetime as _dt
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
APP_VERSION = "2.4"
APP_ID = "ModelLookup"

# 调试开关：设了 ML_CDP_PORT 环境变量才开 WebView2 远程调试（正式包不受影响）
try:
    _dbg_port = int(os.environ.get("ML_CDP_PORT") or 0)
    if _dbg_port:
        webview.settings["REMOTE_DEBUGGING_PORT"] = _dbg_port
except Exception:
    pass
# payload 结构版本：与 parse_kdocs.py 产出 meta.pv 对齐。
# 改了 rows 的字段结构（加字段/改含义）就 +1，老缓存自动失效换用包内数据。
PAYLOAD_VER = 24

ONLINE_SHARE = "https://www.kdocs.cn/l/ch7gwGTP9De4"
FILE_SHARE = "ch7gwGTP9De4"
FILE_ID = "548020345856"

PRICE_SHARE = "https://www.kdocs.cn/l/cficbK9Drrdp"
PRICE_SHARE_CODE = "cficbK9Drrdp"

SOURCES = [
    {"kind": "clean", "name": "公布版 · 滞销样机清理明细",
     "share": FILE_SHARE, "fileId": FILE_ID, "url": ONLINE_SHARE},
    {"kind": "price", "name": "2026年10月价格表 · LYDF（型号价格表 / 美的）",
     "share": PRICE_SHARE_CODE, "fileId": "", "url": PRICE_SHARE},
]

CLEAN_XLSX = "data.xlsx"
PRICE_XLSX = "price.xlsx"

# ---- 启动耗时埋点（设 ML_TIMING=1 才写日志，平时零开销） ----
_T0 = time.time()
_TIMING = []


def _tick(label):
    _TIMING.append((label, time.time() - _T0))
    if not os.environ.get("ML_TIMING"):
        return
    try:
        with io.open(os.path.join(os.environ.get("LOCALAPPDATA") or ".", APP_ID,
                                  "boot_timing.log"), "a", encoding="utf-8") as f:
            f.write("%7.2f s  %s\n" % (time.time() - _T0, label))
    except Exception:
        pass


_tick("常量区/埋点就绪")

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
    """型号归一：截掉中文，并清掉可能残留的尾部分隔符。

    ⚠ 尾部的 (-_/+.,、， 必须 rstrip 掉，否则 BCD-550WGCFDM4WHU1(棕) 会留下拖尾「(」，
      和图片库里的 key 对不上（前端 coreOf 里是同一套逻辑，改一处要同步改另一处）。
    """
    n = norm_model(v)
    m = re.search(r"[\u4e00-\u9fff]", n)
    if m and m.start() >= 3:
        return n[: m.start()].rstrip("(-_/+.,、，")
    return n


# 「全国可发」列的口径（用户 2026-10 定稿）：
#   ⚠ 这一列回答的不是「能不能发」，而是「**能不能发到深惠广莞以外**」——
#      所有型号都能发深圳/惠州/广州/东莞，所以卡片上那个绿色标签永远在。
#   3 = 只发深惠广莞：空白 / 不可以 / 进货小于3年不能发外区 / 进货大于3年可以发外区
#       （「进货大于3年可以发外区」看着像可以，但用户明确归到这一档）
#   1 = 报备后可发全国，看地址：可以 / 报备可发全国
#   2 = 要问过才知道：找品类报备确认 / 一单一询是否能发外地
#   表里冒出没见过的写法 → 保守当 2（原文照显给前端）
NAT_CAT = {
    "可以": 1, "可": 1, "可发": 1, "是": 1, "Y": 1, "y": 1, "1": 1, "1.0": 1,
    "√": 1, "支持": 1, "报备可发全国": 1,
    "不可以": 3, "不可": 3, "否": 3, "N": 3, "n": 3, "0": 3, "0.0": 3, "不": 3,
    "×": 3, "x": 3, "不支持": 3,
    "进货小于3年不能发外区": 3, "进货大于3年可以发外区": 3,
    "找品类报备确认": 2, "一单一询是否能发外地": 2,
}


def nat_kind(v):
    """全国可发 → (原始文本 或 None, 语义码 1/2/3)。见上面 NAT_CAT 注释。"""
    t = None if v is None else (str(v).strip() or None)
    if t is None:
        return None, 3                          # 空白也是「只发深惠广莞」
    t0 = re.sub(r"^[\s\-—－–]+", "", t).replace(" ", "")
    if not t0:
        return None, 3
    if t0 in ("0", "0.0", "0.00"):
        return "不可以", 3
    return t, NAT_CAT.get(t0, 2)


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
    if not t or t in ("-", "--", "—", "无"):
        return None
    try:
        f = float(t)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    except ValueError:
        return t


# ------------------------------------------------------------------ xlsx 解析

def _is_placeholder_label(n):
    """表头格里是纯数字（日期序列号 46299）或日期字符串 → 视为占位，不是列名。"""
    t = (n or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return True
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}( \d{1,2}:\d{2}(:\d{2})?)?", t):
        return True
    return False


def _nk(x):
    """列名归一：去掉所有空白（「价格 含税」↔「价格含税」）。"""
    return re.sub(r"\s+", "", str(x or ""))


def header_map(rows, need=("型号",), scan=4):
    """在前 scan 行里找表头行：返回 (行号0基, {列名:列号0基})。

    表头格里为空 或 是日期/数字占位 的列，从紧下一行同列补名字。
    """
    for ri in range(min(scan, len(rows))):
        names = [sx(x) for x in (rows[ri] or [])]
        if not all(n in names for n in need):
            continue
        nxt = rows[ri + 1] if ri + 1 < len(rows) else []
        col = {}
        for i, n in enumerate(names):
            if n is None or _is_placeholder_label(n):
                v2 = sx(nxt[i]) if i < len(nxt) else None
                if v2 and not _is_placeholder_label(v2):
                    n = v2
            if n:
                col.setdefault(n, i)
        return ri, col
    raise RuntimeError("找不到表头行 need=%s" % (need,))


def col_of(col, *cands):
    """列名模糊匹配（忽略空格），先精确、后包含。

    实际表头里坑不少：「价格 含税」中间有空格；「利润」那列真名是
    「除去提货价与平台扣点后利润，考虑是否可做活动」；「备注」重复出现两次。
    """
    for c in cands:
        for k, v in col.items():
            if _nk(k) == _nk(c):
                return v
    for c in cands:
        for k, v in col.items():
            if _nk(c) and _nk(c) in _nk(k):
                return v
    return None


def rows_of(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


def sc(i, r):
    return sx(r[i]) if (i is not None and i < len(r)) else None


def nc(i, r):
    return num(r[i]) if (i is not None and i < len(r)) else None


def parse_clean(ws, kind):
    """公布版清理明细。kind: 'l'=滞销  's'=样机。"""
    rows = rows_of(ws)
    hi, col = header_map(rows, need=("型号", "品牌"))
    i_mo = col["型号"]
    i_q = col_of(col, "滞销清理数量" if kind == "l" else "样机清理数量")
    i_p = col_of(col, "滞销清理价" if kind == "l" else "样机清理价")
    i_rb = col_of(col, "滞销出库奖励" if kind == "l" else "样机出库奖励", "样机出库存奖励")
    # ⚠ 库存就在这两个 sheet 里（用户明确要求）——滞销看「可用量」，样机看「现存量」，
    #   不再去读「现存量10.4 / 现存量」那两张表
    i_av = col_of(col, "可用量" if kind == "l" else "现存量")
    out = []
    for r in rows[hi + 1:]:
        if not r:
            continue
        mo = sc(i_mo, r)
        if not mo or norm_model(mo) == "型号":
            continue
        if sc(i_q, r) is None and sc(i_p, r) is None:
            continue
        nat_raw, nat_k = nat_kind(sc(col_of(col, "全国可发"), r))
        out.append({
            "kind": kind, "mo": mo,
            "b": sc(col_of(col, "品牌"), r), "cat": sc(col_of(col, "品类"), r),
            "code": sc(col_of(col, "YS物料编码"), r), "n": sc(col_of(col, "YS物料名称"), r),
            "lp": nc(col_of(col, "零售指导价"), r), "nat": nat_raw, "natk": nat_k,
            "q": nc(i_q, r), "p": nc(i_p, r), "rb": nc(i_rb, r), "av": nc(i_av, r),
            "st": sc(col_of(col, "产品状态"), r),
        })
    return out


def parse_price(ws):
    """价格表「型号价格表（只看这个）」。r1 是分组标题行，真表头在 r2，数据 r3+。"""
    rows = rows_of(ws)
    hi, col = header_map(rows, need=("型号", "品牌"))
    i_mo = col["型号"]
    out = []
    for r in rows[hi + 1:]:
        if not r:
            continue
        mo = sc(i_mo, r)
        if not mo or norm_model(mo) == "型号":
            continue
        rec = {
            "mo": mo,
            "b": sc(col_of(col, "品牌"), r), "cat": sc(col_of(col, "品类"), r),
            "code": sc(col_of(col, "编码"), r), "n": sc(col_of(col, "名称"), r),
            "lp": nc(col_of(col, "零售指导价"), r),
            # 价格（⚠ 是价格不是数量）
            "pt": nc(col_of(col, "价格含税"), r),
            "npz": nc(col_of(col, "滞销价格"), r),
            "nyz": nc(col_of(col, "样机价格"), r),
            # 库存不再从金山取（2026-10 改版：全部走用友实时接口）
            # 发货
            "ship": sc(col_of(col, "发货区域"), r),
            "from_src": sc(col_of(col, "货来源"), r),
            # 闲鱼
            "xyid": sc(col_of(col, "闲鱼商品ID"), r),
            "xyp": nc(col_of(col, "闲鱼价格"), r),
            "profit": nc(col_of(col, "利润"), r),
            # 补贴
            "s69": sc(col_of(col, "补贴69码"), r),
            "smax": nc(col_of(col, "补贴最高价格"), r),
            "searn": nc(col_of(col, "补贴可赚"), r),
            "suser": nc(col_of(col, "用户补贴金额"), r),
            "base": nc(col_of(col, "裸价"), r),
            "t2": sc(col_of(col, "产品状态"), r),
        }
        if not any(rec.get(k) not in (None, "") for k in
                   ("pt", "npz", "nyz", "s69", "xyp")):
            continue          # 这一行什么都没，跳过
        out.append(rec)
    return out


# ------------------------------------------------------------------ 品牌归一

# 源表里品牌列偶尔会把品类填进去 —— 实测「科沃斯随手吸 165-2107-0001」的品牌列
# 写的是「吸尘器」，于是品牌筛选里冒出一个「吸尘器 1」。另外美的那边用
# 「美的大家电 / 美的小家电」这种带线的写法，要并成「美的」。
BRAND_ALIAS = {
    "美的大家电": "美的", "美的小家电": "美的", "美的": "美的",
    "容声冰箱": "容声", "容声": "容声",
}
KNOWN_BRANDS = ["卡萨帝", "海尔", "统帅", "美的", "容声", "日立", "科沃斯",
                "东芝", "松下", "西门子", "博世", "小天鹅", "格力", "TCL",
                "海信", "创维", "澳柯玛", "星星", "华凌", "COLMO", "GE",
                "夏普", "三星", "LG", "方太", "老板", "九阳", "苏泊尔"]
NOT_BRAND = ("吸尘", "洗地", "扫地", "擦窗", "冰箱", "洗衣", "空调", "热水器",
             "电视", "净水", "冷柜", "厨电", "小家电", "门锁", "机器人", "配件")


def brand_norm(b, name=None):
    """品牌列 → 干净品牌名。认不出的去名称里找（修「品牌填成品类」的笔误），
    实在认不出就归「其他」，免得品牌筛选里混进一堆噪音。"""
    t = (b or "").strip()
    t = BRAND_ALIAS.get(t, t)
    if t in KNOWN_BRANDS:
        return t
    src = "%s %s" % (t, name or "")
    for k in KNOWN_BRANDS:
        if k in src:
            return k
    for w in NOT_BRAND:
        if w in t:
            return "其他"
    return t or None


# ------------------------------------------------------------------ 美的 / 容声

# 「美的」sheet 是自己维护的固定格式：r1 表头、数据 r2+。列序写死并做校验 ——
# 不能走 header_map，因为表头里「0.016」会被 _is_placeholder_label 当成日期序列号
# 占位符，然后被下一行的 69.5999 顶掉。
MD_LAYOUT = {"cat": 1, "brand": 2, "code": 3, "name": 4, "mo": 5,
             "mh": 6, "m2": 7, "mr": 8, "lp": 9, "jd": 11,
             "note": 13, "xy": 17, "profit": 18}


def parse_meidi(ws):
    """「美的」sheet → 记录列表（kind='m'）。另一套报价体系：
    拿货价（慧买卖）/ 价格（给二批）/ 价格取整 / 零售指导价 / 京东价 / 参考闲鱼价 / 利润。"""
    rows = rows_of(ws)
    if not rows:
        return []
    hdr = [sx(x) for x in (rows[0] or [])]
    i_mo = MD_LAYOUT["mo"]
    if len(hdr) <= max(MD_LAYOUT.values()) or hdr[i_mo] != "型号":
        raise RuntimeError("「美的」sheet 列序变了，请核对表头：%s" % (hdr[:22],))

    def cell(r, k):
        i = MD_LAYOUT[k]
        return r[i] if i < len(r) else None

    out = []
    for r in rows[1:]:
        if not r:
            continue
        mo = sx(cell(r, "mo"))
        if not mo or norm_model(mo) == "型号":
            continue
        out.append({
            "kind": "m", "mo": mo,
            "b": brand_norm(sx(cell(r, "brand")), sx(cell(r, "name"))),
            "cat": sx(cell(r, "cat")),
            "code": sx(cell(r, "code")),
            "n": sx(cell(r, "name")),
            "lp": num(cell(r, "lp")),
            "mh": num(cell(r, "mh")),          # 拿货价（慧买卖）
            "m2": num(cell(r, "m2")),          # 价格（给二批）
            "mr": num(cell(r, "mr")),          # 价格取整
            "jd": num(cell(r, "jd")),          # 京东价或国补后价
            "xyref": num(cell(r, "xy")),       # 参考闲鱼价
            "profit": num(cell(r, "profit")),  # 按参考闲鱼价算的利润
            "note": sx(cell(r, "note")),
        })
    return out


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


def load_records_from_path(path):
    """读一个 xlsx → (记录列表, 月份提示)。自动识别是哪个表。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    names = wb.sheetnames
    base = os.path.basename(path)
    month = ""

    if "滞销" in base or find_sheet(names, lambda n: "滞销清理明细" in n):
        nl = pick_sheet(names, "滞销清理明细", "滞销")
        ns = pick_sheet(names, "样机清理明细", "样机")
        recs = []
        if nl:
            recs += parse_clean(wb[nl], "l")
            m = re.search(r"(\d{1,2})\s*月", nl)
            if m:
                month = "%d-%02d" % (_dt.datetime.now().year, int(m.group(1)))
        if ns:
            recs += parse_clean(wb[ns], "s")
            if not month:
                m = re.search(r"(\d{1,2})\s*月", ns)
                if m:
                    month = "%d-%02d" % (_dt.datetime.now().year, int(m.group(1)))
        return recs, month

    np_ = pick_sheet(names, "型号价格表", "价格表", "只看这个")
    if np_:
        recs = parse_price(wb[np_])
        # 「美的」页：另一套报价体系（美的大家电 / 美的小家电 / 容声）
        if "美的" in names:
            mrecs = parse_meidi(wb["美的"])
            print("  美的 %d 条" % len(mrecs))
            recs += mrecs
        # 「现存量10.6」不再解析（2026-10 改版：库存/仓库全部走用友实时接口）
        return recs, ""

    raise RuntimeError("认不出这个文件是哪个表：" + base)


def merge_records(allrecs):
    """公布版（滞销/样机）+ 价格表 → 按 core 合并成一张卡。"""
    rows = {}

    # ---- 第一批：公布版清理明细
    for rec in [x for x in allrecs if x.get("kind") in ("l", "s")]:
        ck = core_of(rec["mo"])
        r = rows.setdefault(ck, {"c": ck, "mo": rec["mo"], "b": rec["b"], "cat": rec["cat"],
                                 "code": rec["code"], "n": rec["n"], "lp": rec["lp"],
                                 "nat": rec["nat"], "natk": rec.get("natk"), "k": "",
                                 "src_kind": "c"})
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

    # ---- 第二批：价格表
    for rec in [x for x in allrecs if x.get("kind") is None]:
        ck = core_of(rec["mo"])
        r = rows.get(ck)
        if r is None:
            r = rows[ck] = {"c": ck, "mo": rec["mo"], "b": rec["b"], "cat": rec["cat"],
                            "code": rec["code"], "n": rec["n"], "lp": rec["lp"],
                            "nat": None, "natk": None, "k": "", "src_kind": "p"}
        else:
            r["src_kind"] = "cp"
        for k in ("b", "cat", "code", "n", "lp"):
            if r.get(k) in (None, "") and rec.get(k) not in (None, ""):
                r[k] = rec[k]
        r.update(pt=rec["pt"], npz=rec["npz"], nyz=rec["nyz"],
                 ship=rec["ship"], sfrom=rec["from_src"],
                 xyid=rec["xyid"], xyp=rec["xyp"], profit=rec["profit"],
                 s69=rec["s69"], smax=rec["smax"], searn=rec["searn"], suser=rec["suser"],
                 base=rec["base"], t2=rec["t2"])
        if rec.get("mo") and len(str(rec["mo"])) > len(str(r.get("mo") or "")):
            r["mo"] = rec["mo"]

    # ---- 第三批：美的 / 容声（自己那一套报价）
    #   同一型号在这张表里可能出现两次（不同批次报价），取「拿货价·慧买卖」最低的那行；
    #   整行一起换，免得出现「拿货价取的是 A 行、参考闲鱼价取的是 B 行」的错配。
    def _mkey(x):
        return x["mh"] if isinstance(x.get("mh"), (int, float)) else float("inf")
    mdedup = {}
    for rec in [x for x in allrecs if x.get("kind") == "m"]:
        ck = core_of(rec["mo"])
        cur = mdedup.get(ck)
        if cur is None or _mkey(rec) < _mkey(cur):
            mdedup[ck] = rec
    for ck, rec in mdedup.items():
        r = rows.get(ck)
        if r is None:
            r = rows[ck] = {"c": ck, "mo": rec["mo"], "b": rec["b"], "cat": rec["cat"],
                            "code": rec["code"], "n": rec["n"], "lp": rec["lp"],
                            "nat": None, "natk": None, "k": "", "src_kind": "m"}
        else:
            # 主表里也有这个型号 → 只补它没有的字段，别覆盖
            if "m" not in (r.get("src_kind") or ""):
                r["src_kind"] = (r.get("src_kind") or "") + "m"
        for k in ("b", "cat", "code", "n", "lp"):
            if r.get(k) in (None, "") and rec.get(k) not in (None, ""):
                r[k] = rec[k]
        for k in ("mh", "m2", "mr", "jd", "xyref", "profit", "note"):
            if r.get(k) in (None, "") and rec.get(k) not in (None, ""):
                r[k] = rec[k]
        if rec.get("mo") and len(str(rec["mo"])) > len(str(r.get("mo") or "")):
            r["mo"] = rec["mo"]

    # ---- 第四批：官网价快照（打包进 assets 的 midea_prices.json / ronshen_prices.json，
    #   分别抓自美的商城、海信商城，构建期随包更新）。缓存键 = 合并后 rows 的 mo。
    mcache = {}
    for fname in ("midea_prices.json", "ronshen_prices.json"):
        try:
            fp = rp(fname)
            if os.path.isfile(fp):
                with io.open(fp, encoding="utf-8") as f:
                    mcache.update(json.load(f))
        except Exception:
            pass
    nmprice = 0
    for ck, r in rows.items():
        p = mcache.get(r.get("mo"))
        if p and p.get("price") is not None:
            r["mp"] = p["price"]
            if p.get("oprice") is not None:
                r["mop"] = p["oprice"]
            nmprice += 1
    _tick("官网价合并 %d/%d" % (nmprice, len(mcache)))

    # ---- 品牌归一（修「品牌列填成品类」的笔误 + 美的/容声合并）
    for r in rows.values():
        r["b"] = brand_norm(r.get("b"), r.get("n"))
    return rows


# ------------------------------------------------------------------ payload

def load_img_library():
    """全量图库：打包进 assets 的 images_cache.json（core → dataURL）。"""
    p = rp("images_cache.json")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def build_payload(allrecs, imgs_cache, month=""):
    rows = merge_records(allrecs)
    imgs = {ck: v for ck, v in (imgs_cache or {}).items()
            if ck in rows and isinstance(v, str) and v.startswith("data:image/")}
    now = time.strftime("%Y-%m-%d %H:%M")
    return {
        "meta": {"month": month, "src": "", "built": now,
                 "online": {"share": ONLINE_SHARE, "fileId": FILE_ID},
                 "sources": SOURCES},
        "rows": sorted(rows.values(),
                       key=lambda r: (r.get("b") or "", r.get("cat") or "", r["mo"])),
        "imgs": imgs,
    }


_PAY = None
_PAY_LOCK = threading.Lock()
_DATA_JSON = None
_IMG_BYTES = {}


def get_payload():
    global _PAY
    with _PAY_LOCK:
        if _PAY is None:
            _PAY = load_current_payload()
        return _PAY


def set_payload(p):
    global _PAY, _DATA_JSON
    with _PAY_LOCK:
        p.setdefault("meta", {})["pv"] = PAYLOAD_VER
        _PAY = p
        _DATA_JSON = None
        _IMG_BYTES.clear()


def api_data_bytes():
    """给页面的数据：只有 meta + rows + 图片 key 列表（不含图本身）。"""
    global _DATA_JSON
    if _DATA_JSON is None:
        p = get_payload()
        keys = [k for k, v in (p.get("imgs") or {}).items() if isinstance(v, str)]
        body = json.dumps({"meta": p.get("meta") or {}, "rows": p.get("rows") or [],
                           "imgkeys": keys}, ensure_ascii=False, separators=(",", ":"))
        _DATA_JSON = body.encode("utf-8")
    return _DATA_JSON


def img_bytes(core):
    """按需给一张机图（首次从 dataURL 解出来，之后走内存缓存）。"""
    b = _IMG_BYTES.get(core)
    if b is not None:
        return b if b else None, ""
    v = (get_payload().get("imgs") or {}).get(core)
    if not isinstance(v, str) or not v.startswith("data:image/"):
        _IMG_BYTES[core] = b""
        return None, ""
    head, _, b64 = v.partition(",")
    ctype = (head[5:].split(";")[0] or "image/jpeg")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        _IMG_BYTES[core] = b""
        return None, ""
    _IMG_BYTES[core] = raw
    return raw, ctype


def render(payload, template=None):
    """把数据内联进模板 → 离线单文件 HTML（exe 版不用这条路径）。"""
    if template is None:
        with io.open(rp("template.html"), encoding="utf-8") as f:
            template = f.read()
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    raw = raw.replace("</", "<\\/")
    if "/*__DATA__*/null" not in template:
        raise RuntimeError("模板缺少 /*__DATA__*/null 占位")
    return template.replace("/*__DATA__*/null", raw, 1)


def bootstrap():
    """写查询页。exe 版**只写模板**（几十 KB）——数据走 /api/data、图片走 /img/。

    ⚠ 这里以前是把 payload 内联进 HTML，页面 1.9MB，WebView2 光解析首屏就要 4 秒。
      改成「骨架页先行」后窗口 1.5 秒左右就出来了。
    """
    with io.open(rp("template.html"), encoding="utf-8") as f:
        tpl = f.read()
    with io.open(wp("index.html"), "w", encoding="utf-8") as f:
        f.write(tpl)
    return wp("index.html")


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
def sync_xhr_js(file_id):
    return ("(function(){try{"
            "var x=new XMLHttpRequest();"
            "x.open('GET','/api/v3/office/file/" + str(file_id) + "/download',false);"
            "x.send();"
            "return x.status+'|'+String(x.responseText);"
            "}catch(e){return 'ERR|'+e.message;}})()")


# 从分享链接反查 fileId：页面自己会去请求 /api/v3/office/file/<fileId>/…，
# 抓 performance 里的 URL 就能捞出来（比让用户手工填靠谱）
PROBE_FID_JS = (
    "(function(){try{var a=performance.getEntries()||[];var s='';"
    "for(var i=0;i<a.length;i++){s+=String(a[i].name)+'\\n';}"
    "var m=s.match(/\\/file\\/(\\d{6,})/);return m?m[1]:'';}catch(e){return '';}})()"
)

MAIN_WIN = None
LOGIN_WIN = None
LOGIN_CLOSED = threading.Event()
YY_WIN = None
YY_CLOSED = threading.Event()


def js_eval(win, script, default=None):
    if win is None:
        return default
    try:
        return win.evaluate_js(script)
    except Exception:
        return default


def kdocs_ready(win):
    return str(js_eval(win, "location.host", "") or "").endswith("kdocs.cn")


def direct_url_from_window(log, file_id):
    """返回 (直链, 错误)。错误 'NOTLOGIN' 表示窗口没登录。"""
    if LOGIN_WIN is None or LOGIN_CLOSED.is_set():
        return None, "登录窗口不可用"
    if not kdocs_ready(LOGIN_WIN):
        return None, "NOTLOGIN"
    r = js_eval(LOGIN_WIN, sync_xhr_js(file_id))
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


def open_login_window(log, share_url):
    """把登录窗口导航到在线表所在域，等页面就绪。"""
    set_state("running", "正在打开金山文档…")
    log("打开金山文档页面…")
    LOGIN_WIN.load_url(share_url)
    for _ in range(40):
        time.sleep(0.5)
        if LOGIN_CLOSED.is_set():
            return False
        if kdocs_ready(LOGIN_WIN) and js_eval(LOGIN_WIN, "document.readyState") == "complete":
            time.sleep(1.0)
            return True
    return kdocs_ready(LOGIN_WIN)


def obtain_download_url(log, file_id=FILE_ID, share_url=ONLINE_SHARE,
                        allow_prompt=True, wait_s=900, silent=8):
    """拿到 xlsx 的签名直链。已登录会静默完成；未登录则弹窗等人扫码。"""
    if LOGIN_WIN is None or LOGIN_CLOSED.is_set():
        raise RuntimeError("登录窗口不可用，无法从在线表更新")

    if not kdocs_ready(LOGIN_WIN):
        open_login_window(log, share_url)

    # 先静默重试：在线表是公开分享的，页面加载后十几秒匿名会话往往就能拿到直链
    # —— 别一上来就弹扫码窗吓人，绝大部分人根本不用登录
    set_state("running", "正在连接在线表…（如果需要登录，会自动弹出扫码窗口）")
    for i in range(max(silent, 1)):
        du, err = direct_url_from_window(log, file_id)
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
        du, err = direct_url_from_window(log, file_id)
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


def probe_file_id(log, share_url, cache_name="price_fileid.txt"):
    """打开分享页 → 从它自己发出的请求里反查 fileId → 缓存到工作目录。"""
    p = wp(cache_name)
    if os.path.isfile(p):
        try:
            t = io.open(p, encoding="utf-8").read().strip()
            if t.isdigit():
                return t
        except Exception:
            pass
    if LOGIN_WIN is None or LOGIN_CLOSED.is_set():
        return None
    log("正在打开价格表分享页，反查 fileId…")
    LOGIN_WIN.load_url(share_url)
    for _ in range(40):
        time.sleep(0.5)
        if LOGIN_CLOSED.is_set():
            return None
        if kdocs_ready(LOGIN_WIN) and js_eval(LOGIN_WIN, "document.readyState") == "complete":
            break
    time.sleep(1.5)
    fid = str(js_eval(LOGIN_WIN, PROBE_FID_JS) or "").strip()
    if not fid.isdigit():
        log("没探到价格表的 fileId（分享页可能改版了）")
        return None
    try:
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(fid)
    except Exception:
        pass
    log("价格表 fileId = %s" % fid)
    return fid


# ------------------------------------------------------------------ 更新流程

_update_lock = threading.Lock()


def load_current_payload():
    """当前数据：优先工作目录里的（更新后落在那），否则用 exe 内嵌的。

    ⚠ 工作目录缓存带 meta.pv 版本号：与代码里的 PAYLOAD_VER 不一致就视为过期
      （老版本升级上来时字段结构可能变了），自动改用 exe 内嵌的新数据。
    """
    p = wp("payload.json")
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                d = json.load(f)
            if (d.get("meta") or {}).get("pv") == PAYLOAD_VER:
                return d
        except Exception:
            pass
    p = rp("payload.json")
    with io.open(p, encoding="utf-8") as f:
        return json.load(f)


def rebuild(log):
    """用工作目录里的 data.xlsx / price.xlsx 重建 payload。"""
    allrecs, month = [], ""
    for name in (CLEAN_XLSX, PRICE_XLSX):
        p = wp(name)
        if not os.path.isfile(p):
            continue
        try:
            recs, mo = load_records_from_path(p)
            allrecs += recs
            month = month or mo
            log("%s → %d 条记录" % (name, len(recs)))
        except Exception as e:
            log("%s 解析失败：%s" % (name, e))

    if not allrecs:
        raise RuntimeError("没有可用的数据文件（data.xlsx / price.xlsx 都不在）")

    cache = load_img_library()
    old = None
    try:
        old = load_current_payload()
    except Exception:
        pass
    if old:
        merged = dict(cache)
        merged.update(old.get("imgs") or {})      # 老 payload 里的图兜底
        cache = merged

    payload = build_payload(allrecs, cache, month)
    payload["meta"]["src"] = ""
    set_payload(payload)
    with io.open(wp("payload.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)      # 记为「当前数据」，下次启动沿用
    log("重建完成：%d 个型号 / %d 张机图" % (len(payload["rows"]), len(payload["imgs"])))
    set_state(built=payload["meta"]["built"])
    return payload


# ------------------------------------------------------------------ 用友 YonSuite：实时库存（走内嵌窗口，登录一次一直用）

# 数据接口（2026-10-07 实测打通）：
#   POST /yonbip-scm-stock/report/list  condition 给空 = 默认查询（全仓库类型 ~8161 行）
#   必带头：Domain-Key: ustock + X-XSRF-TOKEN（取自 XSRF-TOKEN cookie）
#   在「已经是 c3.yonyoucloud.com 域」的窗口里跑同步 XHR —— 同源自动带登录态，
#   登录一次存在 WebView2 用户目录里，以后点更新直接拉，不用再登录。
# ⚠ 入口必须走带 service 参数的 CAS（sysid=diwork & service=c2/login）：
#   直接开 c3 的话，登录后缺 diwork 的 SSO 票据链，页面能看但 API 全 401（实测踩过）。
YY_CAS_ENTRY = ("https://euc.yonyoucloud.com/cas/login?sysid=diwork&multiCas=true"
                "&service=https%3A%2F%2Fc2.yonyoucloud.com%2Flogin")
YY_LIST_PATH = "/yonbip-scm-stock/report/list?locale=zh_CN&serviceCode=stock_stockanalysislist&terminalType=1"
YY_GROUP_SCHEMA = "2118595716194503889"
YY_TPLID = "2159497746319933444"
YY_K_MODEL = "oid_userDefine_2159497746314166282"   # 型号
YY_K_QTY = "currentqty"                             # 现存量
YY_K_AVAIL = "availableqty"                         # 可用量
YY_K_WHTYPE = "oid_userDefine_2169743811705569285"  # 仓库类型
YY_K_WH = "warehouse_name"                          # 仓库名
YY_K_SKUNAME = "productsku_skuName"                 # 物料名称
YY_PAGE_SIZE = 1000
YY_PAGE_MAX = 15            # 安全上限（8161 行 / 1000 ≈ 9 页）
YY_STOCK_FILE = "stock_live.json"

STOCK = {"ts": None, "rows": 0, "stock": {}}
STOCK_LOCK = threading.Lock()


def stock_load():
    """启动时把上次的实时库存读回内存（没有就当没有）。"""
    global STOCK
    p = wp(YY_STOCK_FILE)
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("stock"), dict):
                STOCK = {"ts": d.get("ts"), "rows": d.get("rows") or 0, "stock": d["stock"]}
        except Exception:
            pass


def stock_save():
    with STOCK_LOCK:
        try:
            with io.open(wp(YY_STOCK_FILE), "w", encoding="utf-8") as f:
                json.dump(STOCK, f, ensure_ascii=False)
        except Exception:
            pass


def _jsq(s):
    """把 Python 字符串安全塞进 JS 单引号字符串字面量。"""
    return str(s).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "")


def yy_fetch_page_js(page_index, page_size=YY_PAGE_SIZE):
    """在 c3 域的窗口里跑：同步 XHR 拉 report/list 一页，返回 responseText。"""
    body = json.dumps({
        "page": {"pageSize": page_size, "pageIndex": page_index},
        "billnum": "stock_stockanalysis",
        "condition": {"commonVOs": []},
        "locale": "zh_CN",
        "serviceCode": "stock_stockanalysislist",
        "groupSchemaId": YY_GROUP_SCHEMA,
        "isIncludeMeta": True,
        "ownDomain": "ustock",
        "tplid": YY_TPLID,
        "bEmptyWithoutFilterTree": False,
    }, ensure_ascii=False)
    return ("(function(){try{"
            "var m=document.cookie.match(/XSRF-TOKEN=([^;]+)/);"
            "var t=m?decodeURIComponent(m[1]):'';"
            "var x=new XMLHttpRequest();"
            "x.open('POST','" + YY_LIST_PATH + "',false);"
            "x.setRequestHeader('Content-Type','application/json;charset=utf-8');"
            "x.setRequestHeader('Domain-Key','ustock');"
            "x.setRequestHeader('X-XSRF-TOKEN',t);"
            "x.send('" + _jsq(body) + "');"
            "return x.status+'|'+String(x.responseText);"
            "}catch(e){return 'ERR|'+e.message;}})()")


def yy_call(win, page_index, page_size=YY_PAGE_SIZE):
    """返回 (data_dict, None) 或 (None, 错误串)。err='NOTLOGIN' 表示没登录。"""
    r = js_eval(win, yy_fetch_page_js(page_index, page_size))
    if r is None:
        return None, "执行脚本失败"
    r = str(r)
    if r.startswith("ERR|"):
        return None, r[4:]
    st, _, txt = r.partition("|")
    txt = txt.strip()
    if not txt.startswith("{"):
        return None, "NOTLOGIN"          # 被重定向到 CAS 登录页了（返回 HTML）
    try:
        d = json.loads(txt)
    except Exception:
        return None, "返回非 JSON：" + txt[:120]
    if str(d.get("code")) != "200":
        msg = str(d.get("message") or ("HTTP " + st))
        if "401" in msg or "登录" in msg or "token" in msg.lower():
            return None, "NOTLOGIN"
        return None, msg
    return d.get("data") or {}, None


def yy_on_login_page(win):
    u = str(js_eval(win, "location.href", "") or "")
    return ("cas/login" in u) or ("c2.yonyoucloud.com" in u and "/login" in u)


def yy_on_c3(win):
    u = str(js_eval(win, "location.href", "") or "")
    return u.startswith("https://c3.yonyoucloud.com")


def do_stock_update(log):
    """拉用友实时库存：已登录静默拉完；未登录弹窗等人登录（登录一次，之后一直有效）。"""
    global STOCK
    if YY_WIN is None or YY_CLOSED.is_set():
        set_state("error", "用友窗口不可用，重启软件再试")
        return
    try:
        set_state("running", "正在连接用友 YonSuite…")
        log("打开用友工作台…")
        try:
            YY_WIN.load_url(YY_CAS_ENTRY)
        except Exception:
            pass
        # 等页面定下来（最多 15 秒）：要么落在 c3（有会话），要么被踢到 CAS 登录页
        ready = False
        for _ in range(30):
            time.sleep(0.5)
            if YY_CLOSED.is_set():
                set_state("error", "用友窗口被关闭，已取消")
                return
            if yy_on_c3(YY_WIN) or yy_on_login_page(YY_WIN):
                ready = True
                break
        if not ready:
            set_state("error", "用友页面加载不出来（检查网络）")
            return

        # 静默探测会话：pageSize=1 打一发
        data, err = yy_call(YY_WIN, 1, 1)
        if err == "NOTLOGIN":
            log("没有有效会话，弹出登录窗口（登录一次，之后一直有效）")
            try:
                YY_WIN.show()
            except Exception:
                pass
            set_state("login", "请在弹出的窗口里登录用友（一次就好，之后点更新直接拉库存）")
            t0 = time.time()
            ok = False
            while time.time() - t0 < 900:
                time.sleep(3)
                if YY_CLOSED.is_set():
                    set_state("error", "登录窗口被关闭，已取消")
                    return
                if yy_on_c3(YY_WIN):
                    data, err = yy_call(YY_WIN, 1, 1)
                    if err != "NOTLOGIN":
                        ok = True
                        break
                elif yy_on_login_page(YY_WIN) and int(time.time() - t0) % 30 == 0:
                    set_state("login", "等待登录中…（登录成功后自动继续，窗口别关）")
            if not ok:
                try:
                    YY_WIN.hide()
                except Exception:
                    pass
                if data is None and err == "NOTLOGIN":
                    set_state("error", "等待登录超时，已取消")
                elif err:
                    set_state("error", "登录后拉取失败：%s" % err)
                return
            log("登录成功，会话已存住（下次不用再登）")
            try:
                YY_WIN.hide()
            except Exception:
                pass
        elif err:
            set_state("error", "用友接口异常：%s" % err)
            return

        # 分页拉全量（默认查询 = 全仓库类型）
        recs = []
        total = None
        page = 1
        while page <= YY_PAGE_MAX:
            data, err = yy_call(YY_WIN, page)
            if err:
                if err == "NOTLOGIN":
                    set_state("error", "拉取中途会话失效，请重新点「更新实时库存」")
                else:
                    set_state("error", "用友接口异常：%s" % err)
                return
            rows = data.get("recordList") or []
            recs.extend(rows)
            total = data.get("recordCount")
            log("实时库存：已拉 %d / %s 行（第 %d 页）" % (len(recs), total, page))
            set_state("running", "实时库存：已拉 %d / %s 行…" % (len(recs), total if total else len(recs)))
            if total and len(recs) >= total:
                break
            if not rows:
                break
            page += 1
            time.sleep(0.4)

        # 按型号聚合（norm_model 与前端 norm() 严格一致，全角/空格/大小写都归一）
        # whs = 每个仓的明细（2026-10 二次改版：卡片只显示「什么仓有几个」，不再显示可用/现存总数）
        agg = {}
        for r0 in recs:
            mo = norm_model(r0.get(YY_K_MODEL))
            if not mo:
                continue
            a = agg.setdefault(mo, {"q": 0.0, "a": 0.0, "wt": {}, "whs": {}})
            try:
                a["q"] += float(r0.get(YY_K_QTY) or 0)
            except (TypeError, ValueError):
                pass
            try:
                av = float(r0.get(YY_K_AVAIL) or 0)
            except (TypeError, ValueError):
                av = 0.0
            a["a"] += av
            wt = str(r0.get(YY_K_WHTYPE) or "").strip() or "其他仓"
            a["wt"][wt] = round(a["wt"].get(wt, 0.0) + av, 2)
            wname = str(r0.get(YY_K_WH) or "").strip() or "未知仓"
            k2 = (wname, wt)
            a["whs"][k2] = round(a["whs"].get(k2, 0.0) + av, 2)

        # whs: {(仓名, 类型): 数量} → 排序列表 [{w, t, n}]（数量多的在前）
        for a in agg.values():
            lst = [{"w": k[0], "t": k[1], "n": v} for k, v in a["whs"].items()]
            lst.sort(key=lambda x: (-x["n"], x["w"]))
            a["whs"] = lst

        # 命中统计：跟当前表里的型号对一下
        hit = 0
        try:
            rows_now = get_payload().get("rows") or []
            for r1 in rows_now:
                if norm_model(r1.get("mo")) in agg:
                    hit += 1
        except Exception:
            pass

        ts = time.strftime("%m-%d %H:%M")
        with STOCK_LOCK:
            STOCK = {"ts": ts, "rows": len(recs), "stock": agg}
        stock_save()
        log("实时库存更新完成：%d 行 · %d 个型号 · 命中表中 %d 个型号" % (len(recs), len(agg), hit))
        set_state("done", "实时库存已更新：%d 行 · 命中表中 %d 个型号（%s）" % (len(recs), hit, ts))
    except Exception as e:
        set_state("error", "实时库存更新失败：%s" % e)


def do_update(log, xlsx_bytes=None, xlsx_path=None):
    if not _update_lock.acquire(blocking=False):
        return False, "已有更新在进行中"
    try:
        if xlsx_bytes is not None:
            set_state("running", "正在解析上传的 Excel…")
            tmp = wp("upload.xlsx")
            with open(tmp, "wb") as f:
                f.write(xlsx_bytes)
            log("使用本地文件（%.2f MB）" % (len(xlsx_bytes) / 1048576.0))
            recs, mo = load_records_from_path(tmp)
            kind = "l" if any(r.get("kind") in ("l", "s") for r in recs) else "p"
            dst = wp(CLEAN_XLSX if kind == "l" else PRICE_XLSX)
            with open(dst, "wb") as f:
                f.write(xlsx_bytes)
            log("已识别为%s，共 %d 条" % ("公布版清理明细" if kind == "l" else "价格表", len(recs)))
        elif xlsx_path:
            set_state("running", "正在解析本地 Excel…")
            log("使用本地文件：%s" % xlsx_path)
            recs, mo = load_records_from_path(xlsx_path)
            kind = "l" if any(r.get("kind") in ("l", "s") for r in recs) else "p"
            import shutil
            shutil.copyfile(xlsx_path, wp(CLEAN_XLSX if kind == "l" else PRICE_XLSX))
        else:
            # ---- ① 公布版
            set_state("running", "正在连接金山文档（公布版）…")
            du = obtain_download_url(log, FILE_ID, ONLINE_SHARE)
            set_state("running", "正在下载公布版在线表…")
            raw = fetch_xlsx_by_url(du, log)
            with open(wp(CLEAN_XLSX), "wb") as f:
                f.write(raw)

            # ---- ② 价格表（fileId 现场反查；拿不到就沿用上次的，不阻断整体更新）
            try:
                set_state("running", "正在连接金山文档（价格表）…")
                fid = probe_file_id(log, PRICE_SHARE)
                if fid:
                    set_state("running", "正在下载价格表…")
                    du2 = obtain_download_url(log, fid, PRICE_SHARE,
                                              allow_prompt=False, silent=3)
                    raw2 = fetch_xlsx_by_url(du2, log)
                    with open(wp(PRICE_XLSX), "wb") as f:
                        f.write(raw2)
                else:
                    log("价格表 fileId 没拿到，保留上一次的价格表数据")
            except Exception as e:
                log("价格表这次没更新成功（%s），保留上一次的数据" % e)

        set_state("running", "正在重建数据…")
        rebuild(log)
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
    server_version = "ModelLookup/2.4"

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
            _tick("WebView 来取页面了")
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

        if path == "/api/data":
            _tick("WebView 来取数据了")
            try:
                self._send(200, api_data_bytes())
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
            return

        if path.startswith("/img/"):
            core = urllib.parse.unquote(path[5:])
            raw, ctype = img_bytes(core)
            if not raw:
                self._send(404, b"", "text/plain; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except Exception:
                pass
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
                    s["built"] = (get_payload().get("meta") or {}).get("built")
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

        if path == "/api/stock":
            with STOCK_LOCK:
                self._send(200, json.dumps(STOCK, ensure_ascii=False))
            return

        if path == "/api/cancel":
            set_state("error", "已取消")
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return

        self._send(404, json.dumps({"ok": False, "msg": "not found"}, ensure_ascii=False))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/stock":
            if read_state()["status"] in ("running", "login"):
                self._send(200, json.dumps({"ok": False, "msg": "正在更新中，请稍候"},
                                           ensure_ascii=False))
                return
            if YY_WIN is None or YY_CLOSED.is_set():
                self._send(200, json.dumps({"ok": False, "msg": "用友窗口不可用，重启软件再试"},
                                           ensure_ascii=False))
                return
            set_state("running", "正在连接用友 YonSuite…")
            threading.Thread(target=do_stock_update, args=(LOG.append,), daemon=True).start()
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
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

# ⚠ 单实例判定**绝对不能用「连一下端口」的办法**：
#   本机（以及不少装了安全软件的机器）连 127.0.0.1 的**未监听端口**不会立刻回 ECONNREFUSED，
#   而是一路等到超时 —— 实测 0.6 秒/个 × 8 个端口 = 4.8 秒，全压在启动路径上，
#   这才是「打开要 5.9 秒」的真正原因（跟页面多大没关系）。
#   改用命名互斥量：CreateMutexW 是瞬时的，进程退出系统自动释放。
_MUTEX = None


def already_running():
    """True = 本机已有本程序在跑（互斥量已被别人持有）。"""
    global _MUTEX
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.CreateMutexW.restype = ctypes.c_void_p
        k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        h = k.CreateMutexW(None, 0, "Local\\ModelLookup_SingleInstance")
        if not h:
            return False
        if k.GetLastError() == 183:        # ERROR_ALREADY_EXISTS
            k.CloseHandle(ctypes.c_void_p(h))
            return True
        _MUTEX = h                          # 拿着不放，进程退出自动释放
        return False
    except Exception:
        return False


def save_port(port):
    try:
        with io.open(wp("port.txt"), "w", encoding="utf-8") as f:
            f.write(str(port))
    except Exception:
        pass


def read_port():
    try:
        t = io.open(wp("port.txt"), encoding="utf-8").read().strip()
        return int(t) if t.isdigit() else 0
    except Exception:
        return 0


def activate_existing(port):
    """把一个已在跑的实例的窗口叫到前面来（连的是**在监听**的端口，秒回）。"""
    for path in ("/api/focus", "/api/ping"):
        try:
            urllib.request.urlopen("http://%s:%d%s" % (HOST, port, path), timeout=1.5).read()
            return True
        except Exception:
            continue
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
    save_port(port)
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

    def upd_stock():
        if YY_WIN is None or YY_CLOSED.is_set():
            webview.windows[0].evaluate_js(
                "alert('用友窗口不可用（可能被关掉了），重启软件再试。')")
            return
        if read_state()["status"] in ("running", "login"):
            return
        threading.Thread(target=do_stock_update, args=(LOG.append,), daemon=True).start()

    def reload_page():
        try:
            webview.windows[0].evaluate_js("location.reload()")
        except Exception:
            pass

    def about():
        webview.windows[0].evaluate_js(
            "alert('型号查询 v%s\\n\\n隆阳电器 · 滞销/样机清理明细（公布版）\\n"
            "数据来源：金山文档在线表（公布版 + LYDF 价格表）\\n内置 %s')"
            % (APP_VERSION, _built_desc())
        )

    def source():
        try:
            import webbrowser
            webbrowser.open(ONLINE_SHARE)
        except Exception:
            pass

    def source_price():
        try:
            import webbrowser
            webbrowser.open(PRICE_SHARE)
        except Exception:
            pass

    return [
        Menu("数据(&D)", [
            MenuAction("从金山文档更新（两个表）", upd),
            MenuAction("从用友更新实时库存", upd_stock),
            MenuAction("重新加载页面", reload_page),
            MenuSeparator(),
            MenuAction("打开在线表（公布版）", source),
            MenuAction("打开在线表（价格表）", source_price),
        ]),
        Menu("帮助(&H)", [
            MenuAction("关于", about),
        ]),
    ]


def _built_desc():
    try:
        p = get_payload()
        return "%d 个型号 / %d 张机图 · 数据 %s" % (
            len(p["rows"]), len(p.get("imgs") or {}), (p.get("meta") or {}).get("built"))
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
    global MAIN_WIN, LOGIN_WIN, YY_WIN
    url = "http://%s:%d/" % (HOST, port)

    _dpi()                                  # 必须在量屏幕 / 建窗前：否则算出来是缩放前的尺寸
    w, h, x, y = fit_window(1480, 940)
    lw, lh, lx, ly = fit_window(1180, 860)

    t_cw = time.time()
    MAIN_WIN = webview.create_window(
        "%s · 隆阳电器（公布版）" % APP_NAME,
        url=url,
        width=w, height=h, x=x, y=y,
        min_size=(900, 600),
        background_color="#f5f7fb",
        text_select=True,
        menu=build_menu(),
    )
    _tick("主窗口对象建好（%.0f ms）" % ((time.time() - t_cw) * 1000))
    t_cw = time.time()
    # 登录窗口：先建好藏着，需要扫码时才 show()（创建必须发生在 start() 之前）
    LOGIN_WIN = webview.create_window(
        "登录金山文档 · 扫码后不用点任何按钮",
        html="<body style='background:#fff;font:14px \"Microsoft YaHei\";padding:30px;color:#5b6577'>"
             "正在打开金山文档…</body>",
        width=lw, height=lh, x=lx, y=ly, min_size=(700, 520), hidden=True,
    )
    _tick("登录窗口对象建好（%.0f ms）" % ((time.time() - t_cw) * 1000))

    def on_login_closed():
        LOGIN_CLOSED.set()

    try:
        LOGIN_WIN.events.closed += on_login_closed
    except Exception:
        pass

    # 用友窗口：同样先建好藏着，点「更新实时库存」需要登录时才 show()
    YY_WIN = webview.create_window(
        "用友登录 · 登录一次，之后点更新直接拉实时库存",
        html="<body style='background:#fff;font:14px \"Microsoft YaHei\";padding:30px;color:#5b6577'>"
             "正在打开用友 YonSuite…</body>",
        width=520, height=680, hidden=True,
    )
    try:
        YY_WIN.events.closed += lambda: YY_CLOSED.set()
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
            if YY_WIN is not None and not YY_CLOSED.is_set():
                YY_WIN.destroy()
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
        _tick("webview.start() 的 GUI 循环已起来")
        LOG.append(lambda m: print("[%s] %s" % (APP_NAME, m)))
        LOG.append(lambda m: None)
        if startup_xlsx:
            time.sleep(1.0)
            do_update(LOG.append, xlsx_path=startup_xlsx)

    _tick("准备调 webview.start()")
    webview.start(after_start, gui="edgechromium", debug=False,
                  private_mode=False, storage_path=wp("webview"),
                  http_server=False)


def main():
    _tick("main() 进入（Python 侧已起来）")
    if not webview2_ok():
        warn_no_webview2()
        return

    # 已经在跑 → 把那个窗口叫到前面来，绝不开第二个
    # （开第二个的话端口/存储路径会分家，用户标的「已发布」「我的定价」就看不见了）
    if already_running():
        p = read_port()
        if not (p and activate_existing(p)):
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(
                    None,
                    "型号查询已经在运行了。\n\n"
                    "如果找不到窗口：看一眼任务栏，\n"
                    "或者在任务管理器里结束 ModelLookup.exe 再重新打开。",
                    APP_NAME, 0x40)
            except Exception:
                pass
        return
    _tick("单实例检查通过")

    arg = None
    for a in sys.argv[1:]:
        if a.lower().endswith((".xlsx", ".xls")) and os.path.isfile(a):
            arg = a
            break

    bootstrap()
    _tick("骨架页写好")
    stock_load()
    _tick("实时库存缓存：%s" % ("上次更新 " + str(STOCK.get("ts")) if STOCK.get("ts") else "还没有"))
    httpd, port = start_server()
    _tick("本地服务已起")
    run_app(httpd, port, arg)


def webview2_ok():
    return _webview2_pv() is not None


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

    for name in ("template.html", "payload.json", "images_cache.json",
                 "midea_prices.json", "ronshen_prices.json"):
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
        p = get_payload()
        n_img = len(p.get("imgs") or {})
        img_ok = all(isinstance(v, str) and v.startswith("data:image/")
                     for v in (p.get("imgs") or {}).values())
        out.append("数据: 型号 %d 个 / 图 %d 张 / dataURL 格式 %s ✅"
                   % (len(p["rows"]), n_img, "正常" if img_ok else "异常"))
        if n_img == 0:
            ok = False
            out.append("数据里一张图都没有 ❌")
        n_src = len((p.get("meta") or {}).get("sources") or [])
        out.append("数据源 %d 个 %s" % (n_src, "✅" if n_src >= 2 else "⚠ 只认得 1 个表"))
        if n_src < 2:
            ok = False
        # 价格表字段抽查
        has_p = sum(1 for r in p["rows"] if r.get("pt") is not None or r.get("npz") is not None)
        out.append("带价格表数据的型号 = %d 个 %s" % (has_p, "✅" if has_p else "❌"))
        if not has_p:
            ok = False
        # 「美的」页并进来了没（另一套报价：拿货价·慧买卖 / 给二批 / 价格取整）
        n_md = sum(1 for r in p["rows"]
                   if any(isinstance(r.get(k), (int, float)) for k in ("mh", "m2", "mr")))
        out.append("美的 / 容声型号 = %d 个 %s" % (n_md, "✅" if n_md else "❌ 美的页没读进来"))
        if not n_md:
            ok = False
        # v24 结构：旧库存字段应已删干净（库存/仓库全部走用友实时接口 stock_live.json）
        n_legacy = sum(1 for r in p["rows"]
                       if r.get("inv") or r.get("pgp") is not None or r.get("psp") is not None)
        out.append("payload 残留旧库存字段(pgp/psp/inv) = %d 个 %s"
                   % (n_legacy, "✅" if not n_legacy else "❌ 还是 v23 旧结构"))
        if n_legacy:
            ok = False
        # 品牌里不该再出现品类名（源表把「科沃斯随手吸」的品牌写成「吸尘器」的那个坑）
        badb = sorted({r.get("b") for r in p["rows"]
                       if r.get("b") and any(w in r["b"] for w in NOT_BRAND)})
        out.append("品牌列残留品类名 = %s %s" % (badb or "无", "✅" if not badb else "❌"))
        if badb:
            ok = False
        # 官网价快照（assets/midea_prices.json + ronshen_prices.json）
        n_mp = sum(1 for r in p["rows"] if isinstance(r.get("mp"), (int, float)))
        out.append("带官网价的型号 = %d 个 %s" % (n_mp, "✅" if n_mp else "⚠ 官网价快照没挂上"))
    except Exception as e:
        ok = False
        out.append("数据读取 ❌ %r" % (e,))

    # 关键提速验证：exe 版页面必须是「骨架页」（不含数据），否则又会退回 5 秒启动
    try:
        bootstrap()
        sz = os.path.getsize(wp("index.html")) / 1024.0
        out.append("查询页 = %s（%.0f KB）%s" % (wp("index.html"), sz,
                                             "✅ 骨架页" if sz < 400 else "❌ 又把数据内联进去了"))
        if sz >= 400:
            ok = False
    except Exception as e:
        ok = False
        out.append("生成查询页 ❌ %r" % (e,))

    try:
        httpd, port = start_server()
        base = "http://%s:%d" % (HOST, port)
        with urllib.request.urlopen(base + "/api/ping", timeout=6) as r:
            d = json.loads(r.read().decode("utf-8"))
        out.append("本地服务 = 端口 %d / app=%s ✅" % (port, d.get("app")))

        with urllib.request.urlopen(base + "/api/data", timeout=20) as r:
            raw = r.read()
        j = json.loads(raw.decode("utf-8"))
        out.append("/api/data = 型号 %d 个 / 图片 key %d 个 / %.2f MB ✅"
                   % (len(j.get("rows") or []), len(j.get("imgkeys") or []),
                      len(raw) / 1048576.0))

        if j.get("imgkeys"):
            k = j["imgkeys"][0]
            with urllib.request.urlopen(base + "/img/" + urllib.parse.quote(k), timeout=20) as r:
                b = r.read()
            out.append("/img/<core> 抽查 = %s / %d 字节 ✅" % (k, len(b)))
            if len(b) < 100:
                ok = False
                out.append("/img/<core> 返回的内容不像图片 ❌")
        else:
            ok = False
            out.append("/api/data 里没有图片 key ❌")
        httpd.shutdown()
    except Exception as e:
        ok = False
        out.append("本地服务 ❌ %r" % (e,))

    if is_frozen():
        mb = os.path.getsize(sys.executable) / 1048576.0
        out.append("exe 体积 = %.2f MB" % mb)
        if mb > 90:
            out.append("⚠ 体积偏大，检查是不是把 numpy/PIL 之类拖进来了")

    out.append("判定：自检%s" % ("通过 ✅" if ok else "失败 ❌"))
    # ASCII 哨兵：CI 拿这行判断成败，不受控制台编码影响
    out.append("RESULT: %s" % ("PASS" if ok else "FAIL"))
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
