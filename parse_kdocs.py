# -*- coding: utf-8 -*-
"""
解析两个金山文档在线表 → 查询页 payload。

① 公布版-滞销样机清理明细（分享码 ch7gwGTP9De4 / fileId 548020345856）
   只读两个 sheet（用户明确要求「其他的都不用读取」）：
     10月滞销清理明细  r1 表头（第 11 列被日期序列号占位，真名「可用量」在 r2 同列）；数据从 r3+
     10月样机清理明细  r1 表头（第 12 列同上，真名「现存量」）；数据从 r3+
   ⚠ 不再读「现存量10.4 / 现存量」—— 库存直接取这两个 sheet 里的「可用量 / 现存量」列。

② 2026年10月价格表-LYDF-共享（分享码 cficbK9Drrdp）—— 读两个 sheet：
   型号价格表（只看这个）  r1 是分组标题行，**真表头在 r2**，数据 r3+（海尔/卡萨帝的全新机报价）
   美的                    另一套报价体系：拿货价（慧买卖）/ 给二批 / 价格取整 / 京东价 / 参考闲鱼价。
                           表头固定 r1、列序写死（表头里「0.016」会被当成占位符，不能走 header_map）
   ⚠ 2026-10 数据源改版：金山只管价格 —— 「现存量10.6」不再解析（仓库明细 inv 已删），
     价格表里的「正品/样机可用库存」两列也不再解析（pgp/psp 已删）。
     库存跟在什么仓库，全部走用友 YonSuite 实时接口（app/main.py do_stock_update → stock_live.json）。

三张表按归一化型号（core_of）合并：同一个型号，清理数据和价格数据合成一张卡。

输出 _payload_kdocs.json：{meta, rows[], imgs{core:dataURL}}
"""
import datetime as _dt
import glob
import io
import json
import os
import re
import sys
from collections import Counter

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build  # noqa: E402

norm_model = build.norm_model
core_of = build.core_of


# ---------------------------------------------------------------- 基础工具

def s(v):
    if v is None:
        return None
    t = str(v).strip()
    return t if t else None


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


# 「全国可发」列的口径（用户 2026-10 定稿）—— 与 app/main.py 里的 NAT_CAT 保持一致：
#   ⚠ 这一列回答的不是「能不能发」，而是「**能不能发到深惠广莞以外**」，
#      所有型号都能发深圳/惠州/广州/东莞。
#   3 = 只发深惠广莞 | 1 = 报备后可发全国（看地址） | 2 = 要问过才知道
NAT_CAT = {
    "可以": 1, "可": 1, "可发": 1, "是": 1, "Y": 1, "y": 1, "1": 1, "1.0": 1,
    "√": 1, "支持": 1, "报备可发全国": 1,
    "不可以": 3, "不可": 3, "否": 3, "N": 3, "n": 3, "0": 3, "0.0": 3, "不": 3,
    "×": 3, "x": 3, "不支持": 3,
    "进货小于3年不能发外区": 3, "进货大于3年可以发外区": 3,
    "找品类报备确认": 2, "一单一询是否能发外地": 2,
}


def nat_kind(v):
    """「全国可发」→ (原始文本 或 None, 语义码 1/2/3)。"""
    t = s(v)
    if t is None:
        return None, 3
    t0 = re.sub(r"^[\s\-—－–]+", "", t).replace(" ", "")
    if not t0:
        return None, 3
    if t0 in ("0", "0.0", "0.00"):
        return "不可以", 3
    return t, NAT_CAT.get(t0, 2)


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
        names = [s(x) for x in (rows[ri] or [])]
        if not all(n in names for n in need):
            continue
        nxt = rows[ri + 1] if ri + 1 < len(rows) else []
        col = {}
        for i, n in enumerate(names):
            if n is None or _is_placeholder_label(n):
                v2 = s(nxt[i]) if i < len(nxt) else None
                if v2 and not _is_placeholder_label(v2):
                    n = v2
            if n:
                col.setdefault(n, i)
        return ri, col
    raise RuntimeError("找不到表头行 need=%s" % (need,))


def col_of(col, *cands):
    """列名模糊匹配（忽略空格与全半角差异），先精确、后包含。

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


def sc(rows, i, r):
    return s(r[i]) if (i is not None and i < len(r)) else None


def nc(rows, i, r):
    return num(r[i]) if (i is not None and i < len(r)) else None


# ---------------------------------------------------------------- ① 清理明细

def parse_clean(ws, kind):
    """kind: 'l'=滞销  's'=样机。返回记录列表。"""
    rows = rows_of(ws)
    hi, col = header_map(rows, need=("型号", "品牌"))
    i_mo = col["型号"]
    i_q = col_of(col, "滞销清理数量" if kind == "l" else "样机清理数量")
    i_p = col_of(col, "滞销清理价" if kind == "l" else "样机清理价")
    i_rb = col_of(col, "滞销出库奖励" if kind == "l" else "样机出库奖励",
                  "样机出库存奖励")
    i_av = col_of(col, "可用量" if kind == "l" else "现存量")
    print("  [%s] 表头行 r%d；可用量列=%s 出库奖励列=%s" % (kind, hi + 1, i_av, i_rb))
    out = []
    for r in rows[hi + 1:]:
        if not r:
            continue
        mo = sc(rows, i_mo, r)
        if not mo or norm_model(mo) == "型号":
            continue
        q, p = nc(rows, i_q, r), nc(rows, i_p, r)
        if not s(r[i_q] if i_q is not None and i_q < len(r) else None) \
           and not s(r[i_p] if i_p is not None and i_p < len(r) else None):
            continue
        nat_raw, nat_k = nat_kind(sc(rows, col_of(col, "全国可发"), r))
        out.append({
            "kind": kind, "mo": mo,
            "b": sc(rows, col_of(col, "品牌"), r),
            "cat": sc(rows, col_of(col, "品类"), r),
            "code": sc(rows, col_of(col, "YS物料编码"), r),
            "n": sc(rows, col_of(col, "YS物料名称"), r),
            "lp": nc(rows, col_of(col, "零售指导价"), r),
            "nat": nat_raw, "natk": nat_k,
            "q": q, "p": p, "rb": nc(rows, i_rb, r), "av": nc(rows, i_av, r),
            "st": sc(rows, col_of(col, "产品状态"), r),
        })
    return out


# ---------------------------------------------------------------- ② 价格表

def parse_price(ws):
    """「型号价格表（只看这个）」→ 记录列表。真表头在 r2，数据 r3+。"""
    rows = rows_of(ws)
    hi, col = header_map(rows, need=("型号", "品牌"))
    i_mo = col["型号"]
    print("  [price] 表头行 r%d，共识别 %d 列" % (hi + 1, len(col)))
    out = []
    for r in rows[hi + 1:]:
        if not r:
            continue
        mo = sc(rows, i_mo, r)
        if not mo or norm_model(mo) == "型号":
            continue
        rec = {
            "mo": mo,
            "b": sc(rows, col_of(col, "品牌"), r),
            "cat": sc(rows, col_of(col, "品类"), r),
            "code": sc(rows, col_of(col, "编码"), r),
            "n": sc(rows, col_of(col, "名称"), r),
            "lp": nc(rows, col_of(col, "零售指导价"), r),
            # 价格：含税价 / 滞销价 / 样机价（⚠ 是价格不是数量）
            "pt": nc(rows, col_of(col, "价格含税"), r),
            "npz": nc(rows, col_of(col, "滞销价格"), r),
            "nyz": nc(rows, col_of(col, "样机价格"), r),
            # 库存不再从金山取（2026-10 改版：全部走用友实时接口）
            # 发货
            "ship": sc(rows, col_of(col, "发货区域"), r),
            "from_src": sc(rows, col_of(col, "货来源"), r),
            # 闲鱼
            "xyid": sc(rows, col_of(col, "闲鱼商品ID"), r),
            "xyp": nc(rows, col_of(col, "闲鱼价格"), r),
            "profit": nc(rows, col_of(col, "利润"), r),
            # 补贴
            "s69": sc(rows, col_of(col, "补贴69码"), r),
            "smax": nc(rows, col_of(col, "补贴最高价格"), r),
            "searn": nc(rows, col_of(col, "补贴可赚"), r),
            "suser": nc(rows, col_of(col, "用户补贴金额"), r),
            "base": nc(rows, col_of(col, "裸价"), r),
            "t2": sc(rows, col_of(col, "产品状态"), r),
        }
        if not any(rec.get(k) not in (None, "") for k in
                   ("pt", "npz", "nyz", "s69", "xyp")):
            continue          # 这一行什么都没，跳过
        out.append(rec)
    return out


# ---------------------------------------------------------------- 品牌归一

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
# 品牌格里出现这些词 = 那格填错了（填成了品类）
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


# ---------------------------------------------------------------- ③ 美的 / 容声

# 「美的」sheet 是自己维护的固定格式：r1 表头、数据 r2+。列序写死并做校验 ——
# 不能走 header_map，因为表头里「0.016」会被 _is_placeholder_label 当成日期序列号
# 占位符，然后被下一行的 69.5999 顶掉。
MD_LAYOUT = {"cat": 1, "brand": 2, "code": 3, "name": 4, "mo": 5,
             "mh": 6, "m2": 7, "mr": 8, "lp": 9, "jd": 11,
             "note": 13, "xy": 17, "profit": 18}


def parse_meidi(ws):
    """「美的」sheet → 记录列表（kind='m'）。这是另一套报价体系：
    拿货价（慧买卖）/ 价格（给二批）/ 价格取整 / 零售指导价 / 京东价 / 参考闲鱼价 / 利润。"""
    rows = rows_of(ws)
    if not rows:
        return []
    hdr = [s(x) for x in (rows[0] or [])]
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
        mo = s(cell(r, "mo"))
        if not mo or norm_model(mo) == "型号":
            continue
        out.append({
            "kind": "m", "mo": mo,
            "b": brand_norm(s(cell(r, "brand")), s(cell(r, "name"))),
            "cat": s(cell(r, "cat")),
            "code": s(cell(r, "code")),
            "n": s(cell(r, "name")),
            "lp": num(cell(r, "lp")),
            "mh": num(cell(r, "mh")),          # 拿货价（慧买卖）
            "m2": num(cell(r, "m2")),          # 价格（给二批）
            "mr": num(cell(r, "mr")),          # 价格取整
            "jd": num(cell(r, "jd")),          # 京东价或国补后价
            "xyref": num(cell(r, "xy")),       # 参考闲鱼价
            "profit": num(cell(r, "profit")),  # 按参考闲鱼价算的利润
            "note": s(cell(r, "note")),
        })
    return out


# ---------------------------------------------------------------- 主流程

def find_sheet(names, pred):
    for n in names:
        if pred(n):
            return n
    return None


def sheet_by_keywords(names, *keys):
    return build.pick_sheet(names, *keys)


def load_rows(path, imgs_cache):
    """读一个 xlsx → 记录列表。公布版和价格表用不同的 sheet 选择规则。"""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    names = wb.sheetnames
    base = os.path.basename(path)
    print("\n源文件: %s\n工作表: %s" % (path, names))

    if "滞销" in base or find_sheet(names, lambda n: "滞销清理明细" in n):
        nl = sheet_by_keywords(names, "滞销清理明细", "滞销")
        ns = sheet_by_keywords(names, "样机清理明细", "样机")
        recs = []
        if nl:
            recs += parse_clean(wb[nl], "l")
        if ns:
            recs += parse_clean(wb[ns], "s")
        n_l = len([x for x in recs if x["kind"] == "l"])
        n_s = len([x for x in recs if x["kind"] == "s"])
        print("  滞销清理 %d 条 / 样机清理 %d 条" % (n_l, n_s))
        return recs

    np_ = sheet_by_keywords(names, "型号价格表", "价格表", "只看这个")
    if np_:
        recs = parse_price(wb[np_])
        print("  价格表 %d 条" % len(recs))
        # 「美的」页：另一套报价体系（用户 2026-10 要求并进来）
        if "美的" in names:
            mrecs = parse_meidi(wb["美的"])
            print("  美的 %d 条" % len(mrecs))
            recs += mrecs
        # 「现存量10.6」不再解析（2026-10 改版：库存/仓库全部走用友实时接口）
        return recs

    raise RuntimeError("认不出这个文件是哪个表：" + base)


def merge(allrecs):
    """公布版（滞销/样机）+ 价格表 → 按 core 合并成一张卡。"""
    rows = {}

    # ---- 第一批：公布版（清理明细）
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

    # ---- 第四批：官网价快照（_midea_fetch.py 抓美的商城、_ronshen_fetch.py 抓海信商城）
    #   缓存键就是合并后 rows 的 mo（抓取脚本读的就是产出 payload），精确对上才挂。
    #   mp = 现售价（元），mop = 划线原价（元）。
    mcache = {}
    for fname in ("midea_prices.json", "ronshen_prices.json"):
        fpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
        if os.path.isfile(fpath):
            try:
                mcache.update(json.load(io.open(fpath, encoding="utf-8")))
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
    print("  官网价命中 %d 个（缓存 %d 条）" % (nmprice, len(mcache)))

    # ---- 品牌归一（修「品牌列填成品类」的笔误 + 美的/容声合并）
    for r in rows.values():
        r["b"] = brand_norm(r.get("b"), r.get("n"))
    return rows


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    clean_src = args[0] if len(args) > 0 else None
    price_src = args[1] if len(args) > 1 else None

    if not clean_src:
        cand = sorted(glob.glob(r"D:\新建文件夹 (2)\公布版-滞销样机清理明细-*.xlsx"))
        cand += [p for p in [os.path.join(os.environ.get("LOCALAPPDATA", ""), "ModelLookup", "data.xlsx")]
                 if os.path.isfile(p)]
        if not cand:
            raise SystemExit("找不到公布版在线表 xlsx")
        clean_src = cand[-1]
    if not price_src:
        cand2 = sorted(glob.glob(r"D:\新建文件夹 (2)\*价格表-LYDF*.xlsx"))
        if cand2:
            price_src = cand2[-1]

    allrecs = load_rows(clean_src, None)
    if price_src and os.path.isfile(price_src):
        allrecs += load_rows(price_src, None)
    else:
        print("⚠ 没有找到价格表 xlsx，只处理公布版")

    rows = merge(allrecs)

    # 图库
    imgp = os.path.join(HERE, "images_cache.json")
    allimg = json.load(io.open(imgp, encoding="utf-8")) if os.path.isfile(imgp) else {}
    imgs = {ck: v for ck, v in allimg.items() if ck in rows and isinstance(v, str)
            and v.startswith("data:image/")}

    m = re.search(r"(\d{4})\D+(\d{1,2})\D", os.path.basename(clean_src))
    month = "%s-%02d" % (m.group(1), int(m.group(2))) if m else ""

    payload = {
        "meta": {
            "month": month,
            "src": os.path.basename(clean_src),
            "built": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "pv": 24,   # payload 结构版本：v24=删pgp/psp/inv（库存全走用友），exe 缓存版本不匹配时自动改用包内数据
            "online": {"share": "https://www.kdocs.cn/l/ch7gwGTP9De4", "fileId": "548020345856"},
            "sources": [
                {"kind": "clean", "name": "公布版 · 滞销样机清理明细",
                 "share": "ch7gwGTP9De4", "fileId": "548020345856",
                 "url": "https://www.kdocs.cn/l/ch7gwGTP9De4"},
                {"kind": "price", "name": "2026年10月价格表 · LYDF",
                 "share": "cficbK9Drrdp", "fileId": "",
                 "url": "https://www.kdocs.cn/l/cficbK9Drrdp"},
            ],
        },
        "rows": sorted(rows.values(), key=lambda r: (r.get("b") or "", r.get("cat") or "", r["mo"])),
        "imgs": imgs,
    }
    with io.open(os.path.join(HERE, "_payload_kdocs.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    # ---- 统计
    bc = Counter(r.get("b") or "?" for r in payload["rows"])
    cc = Counter(r.get("cat") or "?" for r in payload["rows"])
    sk = Counter(r.get("src_kind") or "?" for r in payload["rows"])
    print("-" * 64)
    print("合并后 %d 个型号；有图 %d (%.1f%%)" % (
        len(rows), len(imgs), 100.0 * len(imgs) / max(len(rows), 1)))
    print("来源: 仅公布版 %d / 仅价格表 %d / 两边都有 %d / 美的页 %d / 主表+美的 %d" % (
        sk.get("c", 0), sk.get("p", 0), sk.get("cp", 0),
        sk.get("m", 0), sk.get("pm", 0)))
    unk = {k: v for k, v in bc.items() if k not in ("海尔", "卡萨帝", "统帅", "美的", "容声", "日立", "科沃斯")}
    if unk:
        print("⚠ 没认出来的品牌（已归「其他」或原文）:", dict(Counter(unk).most_common(8)))
    print("品牌:", dict(bc.most_common(8)))
    print("品类(top12):", dict(cc.most_common(12)))
    print("清理样式: 仅滞销 %d / 仅样机 %d / 两者都有 %d" % (
        sum(1 for r in rows.values() if r["k"] == "l"),
        sum(1 for r in rows.values() if r["k"] == "s"),
        sum(1 for r in rows.values() if r["k"] == "ls")))
    for label, key in (("滞销可用量", "lav"), ("样机现存量", "sav")):
        n = sum(1 for r in rows.values() if isinstance(r.get(key), (int, float)))
        print("  %s 有值 %d 个" % (label, n))
    for label, key in (("价格含税", "pt"), ("滞销价格", "npz"), ("样机价格", "nyz"),
                       ("闲鱼价格", "xyp"), ("利润", "profit"),
                       ("补贴最高价格", "smax"), ("补贴可赚", "searn"), ("用户补贴", "suser")):
        vs = [r.get(key) for r in rows.values() if isinstance(r.get(key), (int, float))]
        if vs:
            print("  %s 有值 %d 个, %.0f~%.0f" % (label, len(vs), min(vs), max(vs)))
    ship = Counter(str(r.get("ship")) for r in rows.values() if r.get("ship"))
    print("发货区域取值:", dict(ship.most_common(6)))
    nat = Counter(str(r.get("nat")) for r in rows.values() if r.get("nat"))
    print("全国可发取值:", dict(nat.most_common(6)))
    print("payload → _payload_kdocs.json")


if __name__ == "__main__":
    main()
