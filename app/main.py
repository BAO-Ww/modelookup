# -*- coding: utf-8 -*-
"""
型号查询  v2.7.2
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
import subprocess
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
APP_VERSION = "2.7.8"
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
PAYLOAD_VER = 26

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
        with SLOCK:
            STATE["rev"] += 1          # 数据换新 → 前端轮询到就无感重载


def api_data_bytes():
    """给页面的数据：只有 meta + rows + 图片 key 列表（不含图本身）。"""
    global _DATA_JSON
    if _DATA_JSON is None:
        p = get_payload()
        keys = [k for k, v in (p.get("imgs") or {}).items() if isinstance(v, str)]
        body = json.dumps({"meta": p.get("meta") or {}, "rows": p.get("rows") or [],
                           "imgkeys": keys,
                           "web": p.get("web") or {}}, ensure_ascii=False, separators=(",", ":"))
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

STATE = {"status": "idle", "msg": "", "built": "", "rev": 0,
         "price_at": "", "stock_at": "", "alert_ts": 0, "alert_msg": ""}
SLOCK = threading.Lock()
LOG = []


def set_state(status=None, msg=None, rev=None, built=None,
              price_at=None, stock_at=None, alert_ts=None, alert_msg=None):
    with SLOCK:
        if status is not None:
            STATE["status"] = status
        if msg is not None:
            STATE["msg"] = msg
        if rev is not None:
            STATE["rev"] = rev
        if built is not None:
            STATE["built"] = built
        if price_at is not None:
            STATE["price_at"] = price_at
            try:                            # 2026-10-09：型号库时间持久化，重启不丢
                with io.open(wp("price_ts.txt"), "w", encoding="utf-8") as f:
                    f.write(price_at)
            except Exception:
                pass
        if stock_at is not None:
            STATE["stock_at"] = stock_at
        if alert_ts is not None:
            STATE["alert_ts"] = alert_ts
        if alert_msg is not None:
            STATE["alert_msg"] = alert_msg


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


# ------------------------------------------------------------------ 补贴表（深圳以旧换新集团商品中心）
# 金山分享链接：https://www.kdocs.cn/l/cid8gbTzhB4c（不是公开表 —— 需要金山登录，
# 在软件里登录一次 kdocs 即可，cookie 是持久化的，以后不用再登）。
# 解析：按表头找「型号 / 商品编码 / 零售价」三列，型号归一化后与主表匹配。
SUBSIDY_SHARE = "https://www.kdocs.cn/l/cid8gbTzhB4c"
SUBSIDY_XLSX = "subsidy.xlsx"
SUBSIDY_FID_CACHE = "subsidy_fileid.txt"


def _norm_txt(v):
    return str(v or "").strip()


def parse_subsidy_rows(xlsx_path):
    """以旧换新表 → [{mo(归一), c(69码), p(零售价)}]。"""
    wb = openpyxl.load_workbook(xlsx_path, read_only=True, data_only=True)
    out = []
    try:
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                if any(_norm_txt(c) for c in (row or [])):
                    rows.append(list(row))
                # 83303 行的大表，全量读入（read_only 模式内存可控）
            if not rows:
                continue
            # 找表头行：前 15 行里同时含「型号」「条码/编码」的那行
            # 实表结构：1=商品条码(69码) 2=商品编码(助记码) 10=型号 20=售价
            hi, i_mo, i_code, i_price = None, None, None, None
            for ri, row in enumerate(rows[:15]):
                cells = [_norm_txt(c) for c in row]
                if "型号" in cells and ("商品条码" in cells or "商品编码" in cells):
                    hi = ri
                    for ci, h in enumerate(cells):
                        if h == "型号" and i_mo is None:
                            i_mo = ci
                        elif h == "商品条码" and i_code is None:
                            i_code = ci                     # 69码优先取条码列
                        elif h == "商品编码" and i_code is None:
                            i_code = ci
                        elif (h == "售价" or h == "零售价") and i_price is None:
                            i_price = ci
                    break
            if hi is None or i_mo is None or i_code is None:
                continue
            if i_price is None:
                i_price = i_code + 1 if i_code + 1 < len(rows[hi]) else None
            n = 0
            for row in rows[hi + 1:]:
                mo = norm_model(row[i_mo] if i_mo < len(row) else None)
                code = _norm_txt(row[i_code] if i_code is not None and i_code < len(row) else None)
                if not mo or not code:
                    continue
                pr = None
                if i_price is not None and i_price < len(row):
                    v = row[i_price]
                    if isinstance(v, (int, float)):
                        pr = float(v)
                    else:
                        v2 = re.sub(r"[^\d.]", "", str(v or ""))
                        if v2:
                            try:
                                pr = float(v2)
                            except ValueError:
                                pr = None
                out.append({"mo": mo, "c": code, "p": pr})
                n += 1
            if n:
                break        # 第一个有数据的 sheet 就够
    finally:
        wb.close()
    return out


def attach_subsidy(payload, log):
    """把以旧换新表的 69码+售价 挂到 rows 上（r['sub2']）。匹配不到的型号不动。"""
    p = wp(SUBSIDY_XLSX)
    if not os.path.isfile(p):
        return
    try:
        subs = parse_subsidy_rows(p)
    except Exception as e:
        log("补贴表解析失败：%s（这轮不带补贴数据）" % e)
        return
    if not subs:
        log("补贴表里没解析出数据（列名可能变了）")
        return
    exact = {}
    for s in subs:
        exact.setdefault(s["mo"], s)      # 同型号取第一条
    # 兜底：尾号索引（如主表 CXW-358-C6Q90AGUD ↔ 表里 C6Q90AGUD 这类短型号）
    tails = {}
    for s in subs:
        if len(s["mo"]) >= 6:
            tails.setdefault(s["mo"][-6:], s)
    n_hit = 0
    for r in payload.get("rows") or []:
        k = norm_model(r.get("mo"))
        s = exact.get(k)
        if not s and len(k) >= 6:
            s = tails.get(k[-6:])         # 兜底：主表型号的 6 位尾号对表里的短型号
        if s:
            r["sub2"] = {"c": s["c"], "p": s["p"]}
            n_hit += 1
    log("补贴表：%d 条 → 命中表中 %d 个型号" % (len(subs), n_hit))


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
    attach_subsidy(payload, log)          # 补贴表（以旧换新）：69码 + 售价
    # 官网详情页直链（web 映射）从老 payload 继承，按新 rows 的 key 过滤
    if old and old.get("web"):
        _web = {r.get("c"): old["web"][r.get("c")] for r in payload["rows"] if r.get("c") in old["web"]}
        if _web:
            payload["web"] = _web
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

# 当前登录者的权限（页面登录后 POST /api/perm 上报）—— 原生菜单点按的时候校验用
PERM_STATE = {"u": None, "role": None, "perm": {}}


def has_perm(k):
    """菜单/按钮权限：管理号全开；子账号按云端下发的权限表。"""
    if PERM_STATE.get("role") == "admin":
        return True
    return bool(PERM_STATE.get("perm", {}).get(k))


def price_load():
    """启动时把上次的型号库更新时间读回侧栏（重启不丢）。"""
    p = wp("price_ts.txt")
    if os.path.isfile(p):
        try:
            t = io.open(p, encoding="utf-8").read().strip()
            if t:
                STATE["price_at"] = t
        except Exception:
            pass


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
                if d.get("ts"):
                    STATE["stock_at"] = d["ts"]      # 侧栏一打开就显示上次的库存时间
        except Exception:
            pass


def stock_save():
    with STOCK_LOCK:
        try:
            with io.open(wp(YY_STOCK_FILE), "w", encoding="utf-8") as f:
                json.dump(STOCK, f, ensure_ascii=False)
        except Exception:
            pass


# ------------------------------------------------------------------ 云端中转（Cloudflare Worker）
# 2026-10-08 用户口径：用友库存「我更新了全员跟着更新」——
#   管理号的机器拉用友 → 整包上传 worker（KV stocklive）；子账号不碰用友，直接从云端取。
#   子账号从此**不用登用友**；管理号机器仍是唯一的用友登录点。
CLOUD_HOST = "https://auth.baozang.kdns.fr"
_CLOUD_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
             " (KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _cloud_token():
    """页面登录态镜像，调 worker 用。auth_token.json 兼容两种格式：
    页面 /api/token POST 的是 JSON {"u","token","role"...}；裸 token 字符串也认。"""
    try:
        with io.open(wp("auth_token.json"), encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            return ""
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                return str(d.get("token") or "").strip()
        except Exception:
            pass
        return raw
    except Exception:
        return ""


def cloud_req(path, method="GET", obj=None, timeout=90):
    """调云端 worker（带页面登录态）。返回 (dict|None, err|None)。"""
    tok = _cloud_token()
    if not tok:
        return None, "还没有登录态（页面没登录过）"
    body = json.dumps(obj).encode("utf-8") if obj is not None else None
    req = urllib.request.Request(CLOUD_HOST + path, data=body, method=method)
    req.add_header("X-Token", tok)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", _CLOUD_UA)
    req.add_header("Origin", CLOUD_HOST)
    req.add_header("Referer", CLOUD_HOST + "/")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode("utf-8"))
            return (j, None) if isinstance(j, dict) else (None, "HTTP %d" % e.code)
        except Exception:
            return None, "HTTP %d" % e.code
    except Exception as e:
        return None, str(e)


def yy_alert_push(msg, log=None):
    """用友自动登录失败 → 上报云端告警（管理号软件轮询 /yyalert 能看到）。
    2026-10-08 用户口径：密码不对了要通知管理账户。失败只记日志，不影响主流程。"""
    def _f():
        j, err = cloud_req("/yyalert", "POST",
                           {"msg": "用友登录失败：%s" % msg}, timeout=30)
        if err or not j or not j.get("ok"):
            (log or LOG.append)("⚠ 告警上报云端失败：%s" % (err or (j or {}).get("msg")))
        else:
            (log or LOG.append)("已上报云端告警（管理号会收到）：%s" % msg)
    try:
        threading.Thread(target=_f, daemon=True).start()
    except Exception:
        pass


def yy_alert_poll():
    """管理号轮询云端告警：有新告警（ts 比已知的大）→ 存进 STATE，
    页面 30 秒轮询 /api/state 时读 alert_ts/alert_msg 亮横幅。只管 admin。"""
    if PERM_STATE.get("role") != "admin":
        return
    j, err = cloud_req("/yyalert", "GET", timeout=30)
    if j and j.get("ok"):
        ts = int(j.get("ts") or 0)
        if ts and ts > int(STATE.get("alert_ts") or 0):
            msg = str(j.get("msg") or "")
            who = str(j.get("by") or "")
            set_state(alert_ts=ts, alert_msg=msg + ("（来自 %s）" % who if who else ""))
            LOG.append("🔔 用友告警：%s" % msg)


def stock_push_cloud(log):
    """管理号拉完用友 → 整包上传云端，全员共享。失败只记日志，不影响本机。"""
    with STOCK_LOCK:
        obj = {"ts": STOCK.get("ts"), "rows": STOCK.get("rows"), "stock": STOCK.get("stock")}
    j, err = cloud_req("/stockcloud", "POST", obj, timeout=180)
    if err or not j or not j.get("ok"):
        log("⚠ 库存上传云端失败（子账号拿不到这次的更新）：%s" % (err or (j or {}).get("msg")))
    else:
        log("库存已上传云端（%.1f MB），子账号自动共享" % (j.get("mb") or 0))


def stock_cloud_peek():
    """轻量探云端库存时间戳（给自动轮询用，不拉大数据）。
    返回云端 ts 字符串 / None（网络或数据问题）。"""
    j, err = cloud_req("/stockmeta", "GET", timeout=30)
    if j and j.get("ok"):
        return str(j.get("ts") or "")
    return None


def stock_pull_cloud(log, force=True):
    """子账号：不从用友拉（没有登录态），直接取管理号共享的云端库存。
    force=False（自动轮询用）：云端 ts 跟本地一样就什么都不做（省流量、不打扰）；
    变了才拉全量、落盘、并 bump rev 让页面无感刷新。"""
    global STOCK
    if not force:
        cloud_ts = stock_cloud_peek()
        if not cloud_ts:
            return                        # 探测失败：静默，下轮再说
        with STOCK_LOCK:
            local_ts = STOCK.get("ts") or ""
        if cloud_ts <= local_ts:
            return                        # 云端没有比本地更新的东西（2026-10-09 改成比先后，防止本机新的被云端旧的覆盖）
        log("云端库存有更新（%s → %s），自动同步…" % (local_ts or "无", cloud_ts))
    log("正在从云端同步库存（管理号共享的用友实时数据）…")
    set_state("running", "正在从云端同步库存…")
    j, err = cloud_req("/stockcloud", "GET", timeout=180)
    if err:
        set_state("error", "云端库存同步失败：%s" % err)
        return
    if not j or not j.get("ok") or not isinstance(j.get("stock"), dict):
        set_state("error", (j or {}).get("msg") or "云端还没有库存数据（等管理号更新一次）")
        return
    with STOCK_LOCK:
        STOCK = {"ts": j.get("ts"), "rows": j.get("rows") or 0, "stock": j["stock"]}
    stock_save()
    ts = j.get("ts") or ""
    log("云端库存同步完成：%s 行（%s）" % (j.get("rows") or 0, ts))
    _STK_DETAIL[0] = "从云端同步 %s 行（管理号共享）" % (j.get("rows") or 0)
    if force:
        set_state("done", "库存已同步（云端共享 · %s）" % ts, stock_at=ts)
    else:
        # 自动轮询：bump rev 触发页面无感重载（reloadAll 会重新取 /api/stock）
        set_state("done", "库存已自动同步（云端共享 · %s）" % ts,
                  stock_at=ts, rev=STATE.get("rev", 0) + 1)


def _ver_gt(a, b):
    """版本号比较：'2.7.2' > '2.7.1'。"""
    def nums(s):
        return [int(x) for x in re.findall(r"\d+", str(s) or "")]
    try:
        return nums(a) > nums(b)
    except Exception:
        return False


def sync_xlsx_cloud(log, touch=False):
    """从云端同步金山表格（2026-10-09 v2.8 全云端口径）：
    轻探 /xlsxmeta（ts=各文件内容哈希）→ 有新内容拉 /xlsxcloud 全量 →
    落盘（公布版/价格表/补贴表）→ 重建 → 记 ts。
    touch=True（手动「全部更新」）：即使内容没变也算「云端确认过最新」，刷新侧栏时间；
    自动跟随（touch=False）不刷——不然每 60 秒时间都会动，失去意义。
    返回："synced"（同步并重建完成）/ "unchanged"（云端没新东西）/ 其他 = 错误信息。"""
    try:
        j, err = cloud_req("/xlsxmeta", "GET", timeout=30)
        ts = str((j or {}).get("ts") or "") if (j and j.get("ok")) else ""
        if not ts:
            return "unchanged"           # 云端还没有数据 / 探测失败：当没新东西
        local = ""
        try:
            local = io.open(wp("xlsx_ts.txt"), encoding="utf-8").read().strip()
        except Exception:
            pass
        if ts == local:
            if touch:
                set_state(price_at=time.strftime("%m-%d %H:%M"))
            return "unchanged"
        if not _update_lock.acquire(blocking=False):
            return "busy"                # 别的更新正在跑，这轮先不动
        try:
            log("☁ 云端表格有更新（%s…），自动同步…" % ts[:8])
            set_state("running", "正在从云端同步表格数据…")
            j2, err2 = cloud_req("/xlsxcloud", "GET", timeout=300)
            if err2 or not j2 or not j2.get("ok"):
                msg = "云端表格同步失败：%s" % (err2 or (j2 or {}).get("msg"))
                set_state("error", msg)
                return msg
            files = j2.get("files") or {}
            saved = []
            for name in ("data.xlsx", "price.xlsx", "subsidy.xlsx"):
                b64 = files.get(name)
                if not b64:
                    continue
                raw = base64.b64decode(b64)
                if raw[:2] != b"PK":
                    raise RuntimeError("%s 不是有效的表格文件" % name)
                with open(wp(name), "wb") as f:
                    f.write(raw)
                saved.append(name)
            if not saved:
                return "unchanged"
            set_state("running", "正在重建数据（云端表格）…")
            rebuild(log)
            with io.open(wp("xlsx_ts.txt"), "w", encoding="utf-8") as f:
                f.write(ts)
            set_state("done", "数据已自动同步（云端表格 · %s…）" % ts[:8],
                      price_at=time.strftime("%m-%d %H:%M"))
            log("云端表格同步完成：%s" % ", ".join(saved))
            return "synced"
        finally:
            _update_lock.release()
    except Exception as e:
        try:
            LOG.append("云端表格同步异常：%s" % e)
            set_state("error", "云端表格同步异常：%s" % e)
        except Exception:
            pass
        return "云端表格同步异常：%s" % e


def xlsx_follower():
    """云端表格跟随线程（每 60 秒轻探一次）：GitHub Actions 定时任务（云端浏览器）
    从金山拉「公布版 + 价格表 + 补贴表」传云端；本机有新内容才拉全量重建。
    金山表一更新，哪怕所有软件都关着，云端也会先拿到、开机 90 秒内跟上。"""
    time.sleep(90)                       # 启动头一会儿让位给首屏
    while True:
        try:
            if read_state()["status"] in ("running", "login"):
                time.sleep(60)
                continue
            sync_xlsx_cloud(LOG.append)
        except Exception:
            pass
        time.sleep(60)


def _app_install_bg(log):
    """软件自更新：从云端下安装包 → 静默覆盖安装 → 本进程退出让位（2026-10-08）。
    走 worker 中转（/appdl），客户端不用碰 GitHub。"""
    try:
        j, err = cloud_req("/appcheck", timeout=30)
        if err or not j or not j.get("ok"):
            set_state("error", "查询新版本失败：%s" % (err or (j or {}).get("msg")))
            return
        tag = str(j.get("tag") or "")
        ver = str(j.get("ver") or "")
        log("自更新：开始下载 %s（%.1f MB）…" % (j.get("name") or tag, (j.get("size") or 0) / 1048576.0))
        set_state("running", "正在下载新版本 v%s（几十 MB，视网速 1-3 分钟）…" % ver)
        tok = _cloud_token()
        req = urllib.request.Request(CLOUD_HOST + "/appdl?tag=" + urllib.parse.quote(tag))
        req.add_header("X-Token", tok)
        req.add_header("User-Agent", _CLOUD_UA)
        dest = os.path.join(os.environ.get("TEMP") or wp(""),
                            "ModelLookup-Setup-%s.exe" % (ver or APP_VERSION))
        got, total = 0, 0
        with urllib.request.urlopen(req, timeout=900) as r:
            total = int(r.headers.get("Content-Length") or 0)
            with io.open(dest, "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        set_state("running", "正在下载新版本 v%s… %d%%" % (ver, got * 100 // total))
        if got < 1048576:
            set_state("error", "安装包下载不完整（%d 字节），重试一次" % got)
            return
        log("自更新：下载完成 %s（%.1f MB），准备退出并静默安装…" % (dest, got / 1048576.0))
        set_state("running", "下载完成，软件将自动关闭并安装新版，装完会自动重新打开…")
        # 2026-10-09 修复「下载了但静默安装没落地」：原来是先起安装器、自己 2.5 秒后再退，
        # 安装器解压快的话旧进程还没让位就去覆盖 ModelLookup.exe → 文件被占用，
        # /SUPPRESSMSGBOXES 又吞掉报错窗 → 静默失败什么都不装。
        # 改成交给一个独立的 cmd 帮助进程（DETACHED，跟本进程无父子关系）：
        # 等 2 秒 → 强杀本程序（含 onefile 引导进程）→ 等 1 秒放句柄 →
        # 跑静默安装（带 /LOG 留痕）→ 装完拉起新版。安装器 InitializeSetup
        # 里另有同样的 taskkill 兜底（双保险，手动双击安装也吃这层）。
        logf = os.path.join(os.environ.get("TEMP") or wp(""), "ModelLookup-Setup.log")
        newexe = os.path.join(os.environ.get("LOCALAPPDATA") or "",
                              "Programs", "ModelLookup", "ModelLookup.exe")
        chain = ("ping -n 3 127.0.0.1 >nul"                            # ~2 秒，页面把提示显示完
                 ' & taskkill /F /IM ModelLookup.exe >nul 2>&1'        # 先退出旧实例
                 " & ping -n 2 127.0.0.1 >nul"                         # 等文件句柄释放
                 ' & "%s" /SILENT /SUPPRESSMSGBOXES /NORESTART /LOG="%s"'
                 ' & if exist "%s" start "" "%s"'
                 ) % (dest, logf, newexe, newexe)
        subprocess.Popen(["cmd", "/d", "/c", chain],
                         creationflags=0x00000008)       # DETACHED_PROCESS
        time.sleep(1.5)                      # 帮助进程先起来，页面此时已收到 ok 回应
        try:
            if MAIN_WIN is not None:
                MAIN_WIN.hide()
        except Exception:
            pass
        os._exit(0)                          # 让位给安装器（正常情况已被 taskkill 杀掉，这里是兜底）
    except Exception as e:
        set_state("error", "自更新失败：%s" % e)


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


# ---------------- 免登录（2026-10 三次改版） ----------------
# 之前：CAS 的 TGC 是会话级 cookie，软件一关就没了 → 每次重开都要重新登录。
# 现在：登录成功后把窗口里的全部 cookie（含 HttpOnly 的会话 cookie）抓下来存盘，
#       之后拉库存直接用 urllib 带 Cookie 头访问 c3 API —— 不开窗、不用登录态。
#       会话在服务器端过期时（数天~数周不等）才需要再登一次。
YY_COOKIE_FILE = "yy_cookies.json"
YY_API_HOST = "https://c3.yonyoucloud.com"


def yy_cookies_load():
    p = wp(YY_COOKIE_FILE)
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("cookies"), dict) and d["cookies"]:
                return d
        except Exception:
            pass
    return None


def yy_cookies_save(pairs):
    try:
        with io.open(wp(YY_COOKIE_FILE), "w", encoding="utf-8") as f:
            json.dump({"ts": time.strftime("%m-%d %H:%M"), "cookies": pairs},
                      f, ensure_ascii=False)
    except Exception:
        pass


def yy_capture_cookies(log):
    """登录成功后从用友窗口抓 cookie 存盘（SimpleCookie 列表 → {name: value}）。"""
    try:
        cks = YY_WIN.get_cookies() or []
    except Exception as e:
        log("抓 cookie 失败：%s（重启后可能要重登一次）" % e)
        return
    pairs = {}
    for sc in cks:
        try:
            for name, morsel in sc.items():
                if name and morsel.value:
                    pairs[name] = morsel.value
        except Exception:
            continue
    if pairs:
        yy_cookies_save(pairs)
        log("已保存用友会话（%d 个 cookie）——以后开软件不用再登录" % len(pairs))


def yy_call_direct(page_index, page_size=YY_PAGE_SIZE, timeout=40):
    """不走 WebView：urllib 带保存的 Cookie 直拉 report/list。
    返回 (data, None) / (None, err)，err='NOTLOGIN' 表示会话失效。"""
    saved = yy_cookies_load()
    if not saved:
        return None, "NOTLOGIN"
    pairs = saved["cookies"]
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
    }, ensure_ascii=False).encode("utf-8")
    hdrs = {
        "Content-Type": "application/json;charset=utf-8",
        "Domain-Key": "ustock",
        "Cookie": "; ".join("%s=%s" % (k, v) for k, v in pairs.items()),
        "Referer": YY_API_HOST + "/",
        "Origin": YY_API_HOST,
    }
    xv = pairs.get("XSRF-TOKEN") or ""
    if xv:
        hdrs["X-XSRF-TOKEN"] = urllib.parse.unquote(xv)
    req = urllib.request.Request(YY_API_HOST + YY_LIST_PATH, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) or "login" in (e.url or ""):
            return None, "NOTLOGIN"
        return None, "HTTP %d" % e.code
    except Exception as e:
        return None, str(e) or repr(e)
    txt = txt.strip()
    if not txt.startswith("{"):
        return None, "NOTLOGIN"
    try:
        d = json.loads(txt)
    except Exception:
        return None, "返回非 JSON：" + txt[:120]
    if str(d.get("code")) != "200":
        msg = str(d.get("message") or "接口返回异常")
        if "401" in msg or "登录" in msg or "token" in msg.lower():
            return None, "NOTLOGIN"
        return None, msg
    return d.get("data") or {}, None


def yy_pull_all(call_fn, log):
    """分页拉全量（默认查询 = 全仓库类型）。call_fn(page_index) → (data, err)。"""
    recs = []
    total = None
    page = 1
    while page <= YY_PAGE_MAX:
        data, err = call_fn(page)
        if err:
            return None, err
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
    return recs, None


def yy_finish(recs, log):
    """聚合 + 命中统计 + 落盘。"""
    global STOCK
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
    _STK_DETAIL[0] = "%d 行现存量 · %d 个型号有货 · 命中表中 %d 个，已共享云端" % (len(recs), len(agg), hit)
    set_state("done", "实时库存已更新：%d 行 · 命中表中 %d 个型号（%s）" % (len(recs), hit, ts),
              stock_at=ts)
    stock_push_cloud(log)          # 管理号拉完即上云，子账号全员自动跟进（2026-10-08）


YY_USER = ""     # 用友登录账号（留空 = 从云端/本地配置取；也可写 wp()/yy_auth.json）
YY_PASS = ""


def yy_creds():
    """自动登录用的账密，三级来源（2026-10-08 用户口径：账密挂云端跟管理号走，
    任何一台电脑登着管理号都能担起用友更新）：
    ① 内置常量 ② 本地 wp()/yy_auth.json ③ 云端 worker /yyauth（仅管理号可读，取回后缓存本地）。
    都没有 → None。"""
    if YY_USER and YY_PASS:
        return YY_USER, YY_PASS
    try:
        with io.open(wp("yy_auth.json"), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("u") and d.get("p"):
            return str(d["u"]), str(d["p"])
    except Exception:
        pass
    j, err = cloud_req("/yyauth", timeout=30)
    if j and j.get("ok") and j.get("u") and j.get("p"):
        try:
            with io.open(wp("yy_auth.json"), "w", encoding="utf-8") as f:
                json.dump({"u": j["u"], "p": j["p"]}, f, ensure_ascii=False)
        except Exception:
            pass
        return str(j["u"]), str(j["p"])
    return None


def auto_yy_login(log):
    """用保存的账密在 YY 窗口里自动填 CAS 表单登录（2026-10-08）。
    成功建立会话返回 True；没凭据/没窗口/表单不对/有验证码 → False（调用方按原流程走）。"""
    c = yy_creds()
    if not c:
        return False
    u, p = c
    if YY_WIN is None or YY_CLOSED.is_set():
        return False
    try:
        log("用友会话失效，尝试用保存的账号自动登录…")
        set_state("running", "正在自动登录用友…")
        try:
            YY_WIN.load_url(YY_CAS_ENTRY)
        except Exception:
            pass
        # 等页面定型：要么已经在 c3（会话其实还活着），要么落在 CAS 登录页
        on_login = False
        for _ in range(30):
            time.sleep(0.5)
            if YY_CLOSED.is_set():
                return False
            if yy_on_c3(YY_WIN):
                return True
            if yy_on_login_page(YY_WIN):
                on_login = True
                break
        if not on_login:
            return False
        # 填表提交：用原生 setter 兼容 Vue/React 受控输入
        js = ("(function(){"
              "var u=document.querySelector('input[name=\\'username\\'],input[name=\\'loginName\\'],input[type=\\'text\\'],input[type=\\'email\\']');"
              "var p=document.querySelector('input[name=\\'password\\'],input[type=\\'password\\']');"
              "if(!u||!p) return 'NOFORM';"
              "var setv=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
              "setv.call(u,%s); u.dispatchEvent(new Event('input',{bubbles:true}));"
              "setv.call(p,%s); p.dispatchEvent(new Event('input',{bubbles:true}));"
              "var b=document.querySelector('button[type=\\'submit\\'],input[type=\\'submit\\'],#login-btn,.login-btn,button.btn-primary');"
              "if(b){b.click(); return 'CLICKED';}"
              "var f=u.closest('form'); if(f){f.submit(); return 'SUBMIT';}"
              "return 'NOBTN';})()") % (_jsq(u), _jsq(p))
        r = ""
        try:
            r = YY_WIN.evaluate_js(js) or ""
        except Exception:
            pass
        log("自动登录提交结果：%s" % (r or "NOFORM"))
        if r not in ("CLICKED", "SUBMIT"):
            yy_alert_push("自动填表失败（%s），需要人工登录" % (r or "页面没有登录表单"), log)
            return False
        # 最多等 60 秒跳到 c3；还在登录页多半是密码错或冒了验证码 → 放弃走人工
        for _ in range(120):
            time.sleep(0.5)
            if YY_CLOSED.is_set():
                return False
            if yy_on_c3(YY_WIN):
                yy_capture_cookies(log)
                log("用友自动登录成功，会话已存住")
                return True
        yy_alert_push("自动登录没跳转成功（密码不对或要验证码），需要人工登录", log)
        return False
    except Exception as e:
        log("用友自动登录异常：%s" % e)
        return False


def do_stock_update(log, auto=False):
    """拉用友实时库存：优先用存下的会话直拉（免登录）；失效才弹窗等人登录一次。
    auto=True（定时自动更新）时绝不弹窗：会话失效就这轮跳过，下小时再试。
    2026-10-08 两条新规：
    · 子账号（role != admin）不碰用友，直接取云端共享库存（管理号更新后全员跟进）；
    · 有保存的用友账密（YY_USER/YY_PASS 或 wp()/yy_auth.json）→ 会话失效时自动填表登录。"""
    if not _update_lock.acquire(blocking=False):
        set_state("error", "已有更新在进行中")
        return
    try:
        # ---- 子账号：云端直达（这是 2026-10-08 的主路径，用友只由管理号登录）
        if PERM_STATE.get("role") not in (None, "admin"):
            return stock_pull_cloud(log)

        # ---- 快路径：本地存过用友会话 → urllib 直拉，不开窗、不用登录
        saved = yy_cookies_load()
        if saved:
            set_state("running", "正在连接用友 YonSuite（本地会话）…")
            log("用本地保存的会话直拉（会话存于 %s）" % (saved.get("ts") or "?"))
            data, err = yy_call_direct(1, 1)
            if err is None:
                recs, err = yy_pull_all(yy_call_direct, log)
                if err is None:
                    yy_finish(recs, log)
                    return
            if err != "NOTLOGIN":
                set_state("error", "用友接口异常：%s" % err)
                return
            log("本地会话已失效，改走窗口登录…")

        # ---- 慢路径：WebView 登录流程（登录成功后抓 cookie 存盘，下次走快路径）
        if auto:
            # 自动模式绝不弹窗：先试自动登录（有账密），不行这轮跳过
            if auto_yy_login(log):
                data, err = yy_call_direct(1, 1)
                if err is None:
                    recs, err = yy_pull_all(yy_call_direct, log)
                    if err is None:
                        yy_finish(recs, log)
                        return
                set_state("error", "用友接口异常：%s" % (err or "?"))
                return
            log("自动更新：用友会话失效且自动登录不可用，这轮跳过（下小时再试）")
            set_state("done", "自动更新跳过（下次再试）")
            return
        # 手动点按钮：也先试自动登录（省得人点）；不行再走人工弹窗
        if auto_yy_login(log):
            data, err = yy_call_direct(1, 1)
            if err is None:
                recs, err = yy_pull_all(yy_call_direct, log)
                if err is None:
                    yy_finish(recs, log)
                    return
            set_state("error", "用友接口异常：%s" % (err or "?"))
            return
        if YY_WIN is None or YY_CLOSED.is_set():
            set_state("error", "用友窗口不可用，重启软件再试")
            return
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
            yy_capture_cookies(log)          # ← 关键：把会话 cookie 存下来，重启免登录
            try:
                YY_WIN.hide()
            except Exception:
                pass
        elif err:
            set_state("error", "用友接口异常：%s" % err)
            return

        recs, err = yy_pull_all(lambda p: yy_call(YY_WIN, p), log)
        if err:
            if err == "NOTLOGIN":
                set_state("error", "拉取中途会话失效，请重新点「更新实时库存」")
            else:
                set_state("error", "用友接口异常：%s" % err)
            return
        yy_finish(recs, log)
    except Exception as e:
        set_state("error", "实时库存更新失败：%s" % e)
    finally:
        _update_lock.release()


_ALLRUN = [False, "", "", "", 0, 0.0]   # [在跑, 错误, 摘要, 阶段文案, 估算进度%, 开始时间] —— 进度条用（2026-10-09）

def _all_stage(stage, pct):
    """更新全部更新的阶段文案与估算进度（前端进度条轮询用）。"""
    _ALLRUN[3] = stage
    _ALLRUN[4] = max(int(_ALLRUN[4] or 0), int(pct))   # 只进不退
_UPD_DETAIL = [""]             # 最近一次金山表格更新的明细（do_update 填）
_STK_DETAIL = [""]             # 最近一次用友库存更新的明细（do_stock_update 填）


def _updateall_bg(log, wf="all"):
    """全部更新（2026-10-09 v2.8 全云端口径）：本机不拉数据 —— 一键触发云端
    （GitHub Actions：金山拉表 + 用友拉库存），跑完自动从云端同步下来。
    wf = all | kd（只表格）| yy（只库存）。"""
    errs, parts = [], []
    _ALLRUN[4] = 0
    _ALLRUN[5] = time.time()
    try:
        _all_stage("正在触发云端任务（GitHub Actions）…", 5)
        set_state("running", "正在触发云端更新（GitHub Actions）…")
        j, err = cloud_req("/citrigger", "POST", {"wf": wf}, timeout=60)
        if err or not j or not j.get("ok"):
            raise RuntimeError("触发云端任务失败：%s" % (err or (j or {}).get("msg")))
        tmark = str(j.get("at") or "")[:19]          # ISO UTC 截到秒，比较 run 是否本次触发的
        prev = j.get("prev") or {}                   # 触发前各任务最新 run id（比 tmark 更可靠）
        log("云端任务已触发（%s），等它跑完（一般 1-3 分钟）…" % wf)
        _all_stage("云端任务已触发，等待开跑…", 12)
        res, done = {}, False
        deadline = time.time() + 900
        time.sleep(25)                               # dispatch 后 run 要几秒才出现在列表里
        n_fail, last_err = 0, ""                     # 失败留痕（2026-10-09：之前静默 continue，15 分钟瞎等查不到原因）
        while time.time() < deadline:
            time.sleep(15)
            # 云端一般 1-3 分钟跑完：按已等时长平滑推进 15%→68%（只进不退）
            waited = time.time() - _ALLRUN[5]
            _all_stage("云端拉数据中（已等 %d 分 %02d 秒，一般 1-3 分钟）…"
                       % (int(waited // 60), int(waited % 60)),
                       min(68, 15 + int(waited * 53 / 180.0)))
            js, err2 = cloud_req("/cistatus", "GET", timeout=30)
            if err2 or not js or not js.get("ok"):
                n_fail += 1
                last_err = err2 or (js or {}).get("msg") or "未知"
                _all_stage("云端状态查询失败 %d 次（%s），重试中…"
                           % (n_fail, last_err[:40]), _ALLRUN[4])
                if n_fail % 5 == 0:
                    log("⚠ 查询云端状态第 %d 次失败：%s" % (n_fail, last_err))
                if n_fail >= 40:
                    raise RuntimeError("云端状态连续查询失败 %d 次（最后错误：%s）——"
                                       "检查本机网络/代理（Clash 需给 %s 加直连规则）"
                                       % (n_fail, last_err, CLOUD_HOST.split("//")[-1]))
                continue                             # 网络抖动：下轮再试
            n_fail = 0
            res = js
            want = [k for k in ("yy", "kd")
                    if (k == "yy" and wf in ("all", "yy")) or (k == "kd" and wf in ("all", "kd"))]
            def _is_this_run(k, _res=res, _prev=prev, _tmark=tmark):
                r = _res.get(k) or {}
                if (r.get("status") or "") != "completed":
                    return False
                rid = int(r.get("id") or 0)
                if _prev.get(k) and rid:             # 有触发前 id：run 必须比它新（防止旧 run 误判）
                    return rid > int(_prev[k])
                return str(r.get("at") or "")[:19] >= _tmark
            if all(_is_this_run(k) for k in want):
                done = True
                break
        if not done:
            raise RuntimeError("云端任务等了 15 分钟还没跑完（去 GitHub Actions 页面看看）")
        _all_stage("云端跑完了，正在下载数据到本机…", 72)
        for k, label in (("kd", "表格"), ("yy", "库存")):
            if k not in want_local(wf):
                continue
            r = res.get(k) or {}
            if (r.get("con") or r.get("conclusion") or "") != "success":
                msg = "云端%s任务失败（%s）" % (label, r.get("conclusion") or "未知")
                errs.append(msg)
                parts.append("云端%s：❌ %s" % (label, r.get("conclusion") or "未知"))
                continue
            if k == "kd":
                _all_stage("正在同步云端表格并重建数据（约 20-40 秒）…", 78)
                st = sync_xlsx_cloud(log, touch=True)
                if st == "synced":
                    parts.append("表格：云端已更新，本机已同步重建")
                    _UPD_DETAIL[0] = "云端表格同步重建"
                elif st == "unchanged":
                    parts.append("表格：云端确认过最新（内容无变化，时间已刷新）")
                    _UPD_DETAIL[0] = "云端表格确认最新"
                else:
                    errs.append("表格：%s" % st)
                    parts.append("表格：❌ %s" % st)
            else:
                _all_stage("正在同步云端用友库存…", 88)
                before = (STOCK or {}).get("ts") or ""
                stock_pull_cloud(log, force=False)
                after = (STOCK or {}).get("ts") or ""
                if after and after != before:
                    parts.append("库存：云端已更新，本机已同步（%s）" % after)
                    _STK_DETAIL[0] = "从云端同步 %s 行（云端定时共享）" % (STOCK.get("rows") or 0)
                else:
                    set_state(stock_at=time.strftime("%m-%d %H:%M"))
                    parts.append("库存：云端确认过最新（内容无变化，时间已刷新）")
                    _STK_DETAIL[0] = "云端库存确认最新"
    except Exception as e:
        errs.append(str(e))
        parts.append("❌ %s" % e)
        try:
            set_state("error", str(e))
        except Exception:
            pass
    finally:
        _ALLRUN[0] = False
        _ALLRUN[1] = "；".join(errs)
        _ALLRUN[2] = "　|　".join(parts)
        _ALLRUN[3] = ""
        _ALLRUN[4] = 100
        if not errs:
            set_state("done", "云端更新完成", rev=STATE.get("rev", 0) + 1)
        log("全部更新结束 → %s" % _ALLRUN[2])


def want_local(wf):
    return {"all": ("kd", "yy"), "kd": ("kd",), "yy": ("yy",)}[wf if wf in ("all", "kd", "yy") else "all"]


def do_update(log, xlsx_bytes=None, xlsx_path=None, auto=False):
    _upd_parts = None               # 非 None = 走了金山直拉，记每张表的结果（2026-10-09 摘要用）
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
            # ---- ① 公布版（auto=定时自动更新时绝不弹窗，拿不到直链就这轮跳过）
            set_state("running", "正在连接金山文档（公布版）…")
            du = obtain_download_url(log, FILE_ID, ONLINE_SHARE,
                                     allow_prompt=not auto)
            if not du:
                if auto:
                    log("自动更新：拿不到公布版直链（可能要登录），这轮跳过")
                    set_state("done", "自动更新跳过（下次再试）")
                    return False, "跳过"
                raise RuntimeError("拿不到下载直链")
            set_state("running", "正在下载公布版在线表…")
            raw = fetch_xlsx_by_url(du, log)
            with open(wp(CLEAN_XLSX), "wb") as f:
                f.write(raw)
            _upd_parts = ["公布版✓"]

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
                    _upd_parts.append("价格表✓")
                else:
                    log("价格表 fileId 没拿到，保留上一次的价格表数据")
                    _upd_parts.append("价格表沿用上次")
            except Exception as e:
                log("价格表这次没更新成功（%s），保留上一次的数据" % e)
                _upd_parts.append("价格表沿用上次")

            # ---- ③ 补贴表（以旧换新 69码+售价；这张表要金山登录，没登录就沿用旧数据）
            try:
                set_state("running", "正在连接金山文档（补贴表）…")
                sfid = probe_file_id(log, SUBSIDY_SHARE, cache_name=SUBSIDY_FID_CACHE)
                if sfid:
                    set_state("running", "正在下载补贴表…")
                    du3 = obtain_download_url(log, sfid, SUBSIDY_SHARE,
                                              allow_prompt=True, silent=4)
                    raw3 = fetch_xlsx_by_url(du3, log)
                    with open(wp(SUBSIDY_XLSX), "wb") as f:
                        f.write(raw3)
                    _upd_parts.append("补贴表✓")
                else:
                    log("补贴表 fileId 没拿到，沿用上一次的补贴数据")
                    _upd_parts.append("补贴表沿用上次")
            except Exception as e:
                log("补贴表这次没更新成功（%s），沿用上一次的数据" % e)
                _upd_parts.append("补贴表沿用上次")

        set_state("running", "正在重建数据…")
        _pl = rebuild(log)
        if _upd_parts is not None:
            _UPD_DETAIL[0] = "%s，重建 %d 个型号" % ("、".join(_upd_parts), len(_pl["rows"]))
        else:
            _UPD_DETAIL[0] = "本地文件导入，重建 %d 个型号" % len(_pl["rows"])
        set_state("done", "更新完成", price_at=time.strftime("%m-%d %H:%M"))
        return True, _UPD_DETAIL[0]
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
    server_version = "ModelLookup/2.7.2"

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
            # 告警只给管理号看（子账号 machines 不轮询，字段清空省事）
            if PERM_STATE.get("role") != "admin":
                s["alert_ts"] = 0
                s["alert_msg"] = ""
            self._send(200, json.dumps(s, ensure_ascii=False))
            return

        if path == "/api/updateall":
            # 全部更新（云端版，2026-10-09）：触发 GitHub Actions → 云端跑完自动同步
            # GET = 查进度（页面轮询用）；带 ok=true 的 GET?start=1 或 POST = 启动
            if u.query.startswith("start=1") or self.command == "POST":
                wf = "all"
                if self.command == "POST":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        b = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
                        wf = b.get("wf") if b.get("wf") in ("all", "kd", "yy") else "all"
                    except Exception:
                        pass
                if _ALLRUN[0] or read_state()["status"] in ("running", "login"):
                    self._send(200, json.dumps({"ok": False, "running": True,
                                                "msg": "已有更新在进行中"}, ensure_ascii=False))
                    return
                if PERM_STATE.get("role") not in (None, "admin"):
                    self._send(200, json.dumps({"ok": False, "msg": "只有管理号能触发云端更新（子账号的数据会自动跟着云端走）"},
                                               ensure_ascii=False))
                    return
                _ALLRUN[0] = True
                _ALLRUN[1] = ""
                _ALLRUN[2] = ""
                set_state("running", "正在触发云端更新（GitHub Actions）…")
                threading.Thread(target=_updateall_bg, args=(LOG.append, wf), daemon=True).start()
                self._send(200, json.dumps({"ok": True, "running": True}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"ok": True, "running": _ALLRUN[0],
                                        "had_err": bool(_ALLRUN[1]), "msg": _ALLRUN[1],
                                        "summary": _ALLRUN[2],
                                        "stage": _ALLRUN[3], "pct": _ALLRUN[4],
                                        "elapsed": int(time.time() - _ALLRUN[5]) if _ALLRUN[0] else 0},
                                       ensure_ascii=False))
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

        if path == "/api/appcheck":
            # 软件自更新：查云端（worker 转发 GitHub 最新 Release），跟本机版本比
            j, err = cloud_req("/appcheck", timeout=30)
            if err or not j or not j.get("ok"):
                self._send(200, json.dumps({"ok": False,
                                            "msg": err or (j or {}).get("msg") or "查询失败"},
                                           ensure_ascii=False))
                return
            latest = str(j.get("ver") or "")
            self._send(200, json.dumps({
                "ok": True, "cur": APP_VERSION, "latest": latest, "tag": j.get("tag"),
                "name": j.get("name"), "size": j.get("size"),
                "newer": _ver_gt(latest, APP_VERSION)}, ensure_ascii=False))
            return

        if path == "/api/token":
            # 登录态镜像（读）：页面启动时 localStorage 没有 token 就从这里兜底恢复。
            # 背景：托盘「退出」会硬杀进程，WebView2 的 localStorage 偶发没落盘 → 每次都要重登。
            f = wp("auth_token.json")
            tok = None
            try:
                if os.path.isfile(f):
                    with open(f, encoding="utf-8") as fh:
                        tok = fh.read().strip() or None
            except Exception:
                tok = None
            self._send(200, json.dumps({"token": tok}, ensure_ascii=False))
            return

        self._send(404, json.dumps({"ok": False, "msg": "not found"}, ensure_ascii=False))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/perm":
            # 页面登录后把权限表报上来（原生菜单点按时校验）
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                PERM_STATE["u"] = d.get("u")
                PERM_STATE["role"] = d.get("role")
                PERM_STATE["perm"] = d.get("perm") or {}
                LOG.append("权限已同步：%s（%s）" % (
                    d.get("u") or "?", "管理号" if d.get("role") == "admin" else "子账号"))
                # 2026-10-09 用户要求：标题栏显示当前登录账号
                try:
                    _u = str(d.get("u") or "")
                    _r = "管理号" if d.get("role") == "admin" else "子账号"
                    if MAIN_WIN is not None:
                        MAIN_WIN.set_title("型号查询 — %s（%s）" % (_u, _r))
                except Exception:
                    pass
                # 按角色重画原生菜单：子账号只留 账号管理/退出登录
                threading.Thread(target=rebuild_menu_for,
                                 args=(d.get("role"),), daemon=True).start()
            except Exception as e:
                LOG.append("权限同步失败：%s" % e)
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        if u.path == "/api/appinstall":
            # 一键更新：后台线程下安装包 → 静默覆盖装 → 本进程退出
            if read_state()["status"] in ("running", "login"):
                self._send(200, json.dumps({"ok": False, "msg": "数据更新进行中，等它跑完再更新软件"},
                                           ensure_ascii=False))
                return
            set_state("running", "正在下载新版本安装包…")
            threading.Thread(target=_app_install_bg, args=(LOG.append,), daemon=True).start()
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        if u.path == "/api/token":
            # 登录态镜像（写）：登录时存 token JSON、退出登录时清掉。文件在 wp() 目录。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                f = wp("auth_token.json")
                t = (d.get("token") or "").strip()
                if t:
                    with open(f, "w", encoding="utf-8") as fh:
                        fh.write(t)
                elif os.path.isfile(f):
                    os.remove(f)
            except Exception as e:
                LOG.append("登录态镜像失败：%s" % e)
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
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
        if u.path == "/api/updateall":
            # 全部更新（POST 启动，云端版 2026-10-09）：触发 GitHub Actions → 云端跑完自动同步
            wf = "all"
            try:
                n = int(self.headers.get("Content-Length") or 0)
                b = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}") if n else {}
                wf = b.get("wf") if b.get("wf") in ("all", "kd", "yy") else "all"
            except Exception:
                pass
            if read_state()["status"] in ("running", "login") or _ALLRUN[0]:
                self._send(200, json.dumps({"ok": False, "running": True,
                                            "msg": "已有更新在进行中"}, ensure_ascii=False))
                return
            if PERM_STATE.get("role") not in (None, "admin"):
                self._send(200, json.dumps({"ok": False, "msg": "只有管理号能触发云端更新（子账号的数据会自动跟着云端走）"},
                                           ensure_ascii=False))
                return
            _ALLRUN[0] = True
            _ALLRUN[1] = ""
            _ALLRUN[2] = ""
            set_state("running", "正在触发云端更新（GitHub Actions）…")
            threading.Thread(target=_updateall_bg, args=(LOG.append, wf), daemon=True).start()
            self._send(200, json.dumps({"ok": True, "running": True}, ensure_ascii=False))
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


def _wire_menu_autoclose():
    """原生菜单鼠标移出自动收起（2026-10-09 用户要求：点开菜单后鼠标离开老是不收）。
    给每个顶级菜单项的下拉挂 MouseLeave → Close；菜单重画后必须重新挂。"""
    bv = getattr(MAIN_WIN, "native", None)
    if bv is None:
        return

    def _do():
        try:
            import System.Windows.Forms as WF
            n = 0
            for c in list(bv.Controls):
                if not isinstance(c, WF.MenuStrip):
                    continue
                for item in c.Items:
                    if isinstance(item, WF.ToolStripMenuItem) and item.HasDropDownItems:
                        dd = item.DropDown

                        def _close(sender=None, e=None, _dd=dd):
                            try:
                                _dd.Close()
                            except Exception:
                                pass

                        dd.MouseLeave += _close
                        n += 1
            if n:
                LOG.append("菜单自动收起已挂到 %d 个下拉" % n)
        except Exception as e:
            LOG.append("菜单自动收起挂接失败：%s" % e)

    try:
        from System import Func, Type
        if bv.InvokeRequired:
            bv.Invoke(Func[Type](_do))
        else:
            _do()
    except Exception:
        _do()


def rebuild_menu_for(role):
    """登录后按角色重画原生菜单（2026-10 八次改版）：
    子账号只留 账号管理/退出登录，管理员恢复全量。winforms 的 set_window_menu
    只会往 Controls 里加 MenuStrip，重画前要把旧的摘掉。"""
    try:
        bv = getattr(MAIN_WIN, "native", None)
        if bv is None:
            return
        menus = build_menu(sub=(role != "admin"))

        def _clear():
            try:
                import System.Windows.Forms as WF
                for c in list(bv.Controls):
                    if isinstance(c, WF.MenuStrip):
                        bv.Controls.Remove(c)
            except Exception:
                pass

        try:
            from System import Func, Type
            if bv.InvokeRequired:
                bv.Invoke(Func[Type](_clear))   # winforms 后端同款写法
            else:
                _clear()
        except Exception:
            try:
                _clear()
            except Exception:
                pass
        bv.set_window_menu(menus)
        _wire_menu_autoclose()
        LOG.append("菜单已按角色重画：%s" % ("管理号全量" if role == "admin" else "子账号精简"))
    except Exception as e:
        LOG.append("菜单重画失败：%s" % e)


def build_menu(sub=False):
    """sub=True 返回子账号的精简菜单（只有 账号管理/退出登录，用户要求）。"""
    from webview.menu import Menu, MenuAction, MenuSeparator

    def guarded(name, need):
        """菜单点按先过权限：不满足就弹提示（菜单是原生建的，创建后没法动态藏，
        所以用「点了没权限就拦」的方式，效果一样——子账号点不动）。"""
        if not has_perm(need):
            webview.windows[0].evaluate_js(
                "alert('「%s」只有管理员能用，请联系管理员。')" % name)
            return False
        return True

    def upd_all():
        if not guarded("全部更新", "upd_kdocs"):
            return
        js_click("allbtn")

    def upd():
        if not guarded("从金山文档更新", "upd_kdocs"):
            return
        # 2026-10-09 v2.8 全云端口径：菜单 = 触发云端（GitHub Actions 拉金山），
        # 跑完本机自动同步重建；进度/结果都显示在侧栏底部（startCloud）
        try:
            webview.windows[0].evaluate_js("startCloud('kd')")
        except Exception:
            pass

    def upd_stock():
        # 2026-10-09 v2.8：同上 —— 触发云端（GitHub Actions 拉用友），跑完自动同步
        try:
            webview.windows[0].evaluate_js("startCloud('yy')")
        except Exception:
            pass

    def upd_app():
        """软件自更新：菜单点一下 = 页面里点「⬆ 软件更新」按钮（下载+静默覆盖装）。"""
        js_click("appupd")

    def reload_page():
        if not guarded("重新加载页面", "upd_kdocs"):
            return
        try:
            webview.windows[0].evaluate_js("location.reload()")
        except Exception:
            pass

    def about():
        webview.windows[0].evaluate_js(
            "alert('型号查询 v%s\\n\\n滞销/样机清理明细\\n"
            "数据来源：金山文档在线表（公布版 + LYDF 价格表）\\n内置 %s')"
            % (APP_VERSION, _built_desc())
        )

    def source():
        if not guarded("打开在线表（公布版）", "upd_kdocs"):
            return
        try:
            import webbrowser
            webbrowser.open(ONLINE_SHARE)
        except Exception:
            pass

    def source_price():
        if not guarded("打开在线表（价格表）", "upd_kdocs"):
            return
        try:
            import webbrowser
            webbrowser.open(PRICE_SHARE)
        except Exception:
            pass

    def js_click(elid):
        webview.windows[0].evaluate_js(
            "(function(){var b=document.getElementById('%s'); if(b) b.click();})()" % elid)

    def adm():
        # 管理员 → openAdmin 全量面板；子账号 → openSelf 改密小面板（模板里按角色分流）
        js_click("admbtn")

    def logout():
        js_click("whoami")

    def exp_img():
        if not guarded("导出我补的图", "addimg"):
            return
        js_click("expimg")

    def imp_img():
        if not guarded("导入补图", "addimg"):
            return
        js_click("impimg")

    # 子账号（2026-10-08 十次改版）：菜单 = 账号管理 / 软件更新 / 退出登录
    if sub:
        return [
            MenuAction("账号管理", adm),
            MenuAction("软件更新", upd_app),
            MenuAction("退出登录", logout),
        ]
    # 2026-10-08 顶部菜单归类（用户要求：平铺 9 项太乱，收成三个下拉）：
    #   数据(D) = 页面/在线表/数据更新/补图导入导出；账号(A) = 账号管理/退出登录；帮助(H) = 软件更新/关于
    return [
        Menu("数据(&D)", [
            MenuAction("全部更新", upd_all),
            MenuAction("重新加载页面", reload_page),
            MenuSeparator(),
            MenuAction("从金山文档更新数据", upd),
            MenuAction("更新实时库存", upd_stock),
            MenuSeparator(),
            MenuAction("打开在线表（公布版）", source),
            MenuAction("打开在线表（价格表）", source_price),
            MenuSeparator(),
            MenuAction("导出我补的图", exp_img),
            MenuAction("导入补图", imp_img),
        ]),
        Menu("账号(&A)", [
            MenuAction("账号管理", adm),
            MenuAction("退出登录", logout),
        ]),
        Menu("帮助(&H)", [
            MenuAction("软件更新", upd_app),
            MenuSeparator(),
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


def auto_updater():
    """云端库存跟随线程（2026-10-09 v2.8 全云端口径）：
    本机每小时直拉金山/用友的自动更新已全部取消 —— 定时更新全在云端
    （GitHub Actions，北京时间 8:00-24:00 每小时，凌晨不跑）。
    本机只做 60 秒轻探：云端有比本地新的库存就自动同步下来，所有角色统一走这条；
    金山表格的跟随在 xlsx_follower（也是 60 秒）。软件活着就在跟，托盘/最小化不影响。"""
    time.sleep(180)                      # 启动头几分钟让位给首屏加载
    while True:
        try:
            if read_state()["status"] not in ("running", "login"):
                stock_pull_cloud(LOG.append, force=False)
        except Exception as e:
            LOG.append("自动同步（云端库存）异常：%s" % e)
        time.sleep(60)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_enable(log=None):
    """开机自启（HKCU Run 键，只对当前用户，无需管理员）。
    已存在同名项就不重复写。失败只记日志，不影响启动。"""
    try:
        import winreg
        if is_frozen():
            exe = os.path.abspath(sys.argv[0])          # 打包后 = ModelLookup.exe 本体
        else:
            exe = sys.executable                        # dev 调试用 python.exe
        val = '"%s"' % exe
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            try:
                old, _ = winreg.QueryValueEx(k, APP_NAME)
                if old == val:
                    return True          # 已配置好
            except FileNotFoundError:
                pass
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, val)
        if log:
            log("已设置开机自启：%s" % val)
        return True
    except Exception as e:
        if log:
            log("开机自启设置失败（不影响使用）：%s" % e)
        return False


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


def yy_alert_loop():
    """管理号专用后台线程：每 60 秒轮询云端用友告警（子账号机器不查）。
    告警由别的机器的用友自动登录失败产生 → 这里收到后存 STATE，页面亮横幅。
    挂在 auto_updater 之前启动（登录前 PERM_STATE.role 还是 None，进了循环
    每轮现读，登录后下一轮自动生效）。"""
    time.sleep(90)                       # 启动缓一缓，等登录态就位
    while True:
        try:
            yy_alert_poll()
        except Exception:
            pass
        time.sleep(60)


def run_app(httpd, port, startup_xlsx=None):
    global MAIN_WIN, LOGIN_WIN, YY_WIN
    url = "http://%s:%d/" % (HOST, port)

    class _JsBridge:
        """页面 → Python 的桥：Ctrl+W 在 WebView2 里拦不到（键盘直达 Chromium 子窗口，
        WinForms KeyPreview 无效），由页面 keydown 调这里的 hide_main() 收进托盘。"""
        def hide_main(self):
            try:
                MAIN_WIN.hide()
            except Exception:
                pass

    _dpi()                                  # 必须在量屏幕 / 建窗前：否则算出来是缩放前的尺寸
    w, h, x, y = fit_window(1480, 940)
    lw, lh, lx, ly = fit_window(1180, 860)

    t_cw = time.time()
    MAIN_WIN = webview.create_window(
        APP_NAME,
        url=url,
        width=w, height=h, x=x, y=y,
        min_size=(900, 600),
        background_color="#f5f7fb",
        text_select=True,
        menu=build_menu(),
        js_api=_JsBridge(),
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
    # 定时自动更新（金山价格 + 用友库存，每小时一轮，静默）
    threading.Thread(target=auto_updater, daemon=True).start()
    # 云端表格跟随（GitHub Actions 定时拉金山传云端 → 本机 60 秒轻探自动重建）
    threading.Thread(target=xlsx_follower, daemon=True).start()
    threading.Thread(target=yy_alert_loop, daemon=True).start()   # 管理号轮询用友告警

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

    _REAL_EXIT = [False]        # 真·退出标志（托盘「退出」才置 True）

    def setup_tray():
        """托盘：点 × / Ctrl+W = 收进托盘不退出；托盘双击恢复；托盘菜单「退出」真退。
        winforms 层直接挂 FormClosing / KeyDown（KeyPreview 让 Ctrl+W 先于 WebView2 被拦下）。"""
        try:
            import System.Windows.Forms as WF
            import System.Drawing as DG
            bv = getattr(MAIN_WIN, "native", None)
            if bv is None:
                _tick("托盘：拿不到原生窗口，跳过")
                return
            ico_path = rp("icon.ico")
            ic = DG.Icon(ico_path) if os.path.isfile(ico_path) else DG.SystemIcons.Application

            ni = WF.NotifyIcon()
            ni.Icon = ic
            ni.Text = "型号查询（双击打开）"
            ni.Visible = True

            def _show(sender=None, e=None):
                try:
                    bv.Show()
                    bv.Activate()
                except Exception:
                    pass

            def _real_quit(sender=None, e=None):
                _REAL_EXIT[0] = True
                try:
                    ni.Visible = False
                    ni.Dispose()
                except Exception:
                    pass
                try:
                    httpd.shutdown()
                except Exception:
                    pass
                # 先优雅关窗（给 WebView2 半秒把 localStorage 落盘，登录态才保得住），再兜底硬退
                try:
                    bv.Close()
                except Exception:
                    pass
                time.sleep(0.8)
                os._exit(0)          # 后台线程（自动更新等）一并干净走人

            def _autostart_toggle(sender=None, e=None):
                """托盘菜单「开机自启」勾选开关。"""
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as k:
                        winreg.QueryValueEx(k, APP_NAME)
                    has = True
                except FileNotFoundError:
                    has = False
                except Exception:
                    has = False
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                        if has:
                            winreg.DeleteValue(k, APP_NAME)
                        else:
                            exe = os.path.abspath(sys.argv[0]) if is_frozen() else sys.executable
                            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
                    mi3.Checked = not has
                except Exception:
                    pass

            menu = WF.ContextMenuStrip()
            mi1 = WF.ToolStripMenuItem("打开 型号查询")
            mi1.add_Click(WF.EventHandler(_show))
            mi3 = WF.ToolStripMenuItem("开机自启")
            mi3.CheckOnClick = True
            try:
                import winreg as _wr
                with _wr.OpenKey(_wr.HKEY_CURRENT_USER, RUN_KEY, 0, _wr.KEY_QUERY_VALUE) as k:
                    _wr.QueryValueEx(k, APP_NAME)
                mi3.Checked = True
            except Exception:
                mi3.Checked = False
            mi3.add_Click(WF.EventHandler(_autostart_toggle))
            mi2 = WF.ToolStripMenuItem("退出")
            mi2.add_Click(WF.EventHandler(_real_quit))
            menu.Items.Add(mi1)
            menu.Items.Add(mi3)
            menu.Items.Add(mi2)
            ni.ContextMenuStrip = menu
            ni.add_DoubleClick(WF.EventHandler(_show))

            tipped = [False]

            def _hide_to_tray():
                try:
                    bv.Hide()
                except Exception:
                    pass
                if not tipped[0]:
                    tipped[0] = True
                    try:
                        ni.BalloonTipTitle = "型号查询还在运行"
                        ni.BalloonTipText = "已收进托盘：双击托盘图标打开。点 × 或 Ctrl+W 都只是收起来，退出请用托盘右键「退出」。"
                        ni.ShowBalloonTip(3000)
                    except Exception:
                        pass

            def _on_closing(sender, e):
                if _REAL_EXIT[0]:
                    return
                e.Cancel = True
                _hide_to_tray()

            def _on_keydown(sender, e):
                try:
                    if e.Control and e.KeyCode == WF.Keys.W:
                        e.Handled = True
                        e.SuppressKeyPress = True
                        _hide_to_tray()
                except Exception:
                    pass

            bv.add_FormClosing(WF.FormClosingEventHandler(_on_closing))
            bv.KeyPreview = True
            bv.add_KeyDown(WF.KeyEventHandler(_on_keydown))
            _tick("托盘就绪：× / Ctrl+W 收进托盘，托盘双击恢复，右键退出")
        except Exception as e:
            _tick("托盘初始化失败（不影响其他功能）: %r" % e)

    def after_start():
        _tick("webview.start() 的 GUI 循环已起来")
        LOG.append(lambda m: print("[%s] %s" % (APP_NAME, m)))
        LOG.append(lambda m: None)
        setup_tray()
        time.sleep(1.5)                      # 等 GUI 把初始菜单建完
        _wire_menu_autoclose()
        # 开机自启（2026-10-08）：管理号机器要常驻后台每小时推库存，子账号要自动跟随云端。
        # 全默认开启；托盘菜单里有「取消开机自启」可以关。
        threading.Thread(target=autostart_enable, args=(LOG.append,), daemon=True).start()
        if startup_xlsx:
            time.sleep(1.0)
            do_update(LOG.append, xlsx_path=startup_xlsx)

    _tick("准备调 webview.start()")
    # 运维口子：设 MODELLOOKUP_CDP_PORT=9333 再启动，可远程调试页面
    # （_kdauth.py 抓金山登录态就靠它；pywebview 走 settings 传给 WebView2 —— 2026-10-09）
    cdp_port = (os.environ.get("MODELLOOKUP_CDP_PORT") or "").strip()
    if cdp_port.isdigit():
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(cdp_port)
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
    price_load()
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
