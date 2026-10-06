# -*- coding: utf-8 -*-
"""
解析金山文档「公布版-滞销样机清理明细」在线表 → 查询页 payload。

表结构（2026-10 版，实测）：
  10月滞销清理明细  r1 表头（第 11 列被日期序列号 46299 占位，真名「可用量」在 r2 同列）；数据从 r3+
  10月样机清理明细  r1 表头（第 12 列同上，真名「现存量」）；数据从 r3+
  现存量10.4        r1 r2 两行重复表头，数据从 r3+；第 11 列后是人工辅助列（忽略）
  现存量            r1 表头，数据从 r2+
  预售 / Sheet*     暂不纳入

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
    if not t or t in ("-", "--", "—"):
        return None
    try:
        f = float(t)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    except ValueError:
        return t


def nat_kind(v):
    """「全国可发」是三态以上业务口径 → 返回 (展示原文, 语义)。

    语义：1=可发  0=不可发  2=需人工确认  None=空
    实测取值（2026-10）：
      不可以 / 0(数字零)                      → 0
      可以 / 报备可发全国 / 进货大于3年可以发外区 → 1
      找品类报备确认 / 一单一询是否能发外地 / 其他没见过 → 2
    """
    t = s(v)
    if t is None:
        return None, None
    t0 = t.replace(" ", "")
    if t0 in ("可以", "可", "可发", "是", "Y", "y", "1", "1.0", "√", "支持",
              "报备可发全国", "进货大于3年可以发外区"):
        return t, 1
    if t0 in ("不可以", "不可", "否", "N", "n", "0", "0.0", "不", "×", "x", "不支持",
              "进货小于3年不能发外区"):
        return ("不可以" if t0 in ("0", "0.0") else t), 0
    return t, 2


def _is_num_label(n):
    """表头格里是纯数字（如日期序列号 46299）→ 视为占位，不是列名。"""
    return bool(re.fullmatch(r"\d+(\.\d+)?", (n or "").strip()))


def header_map(rows, need=("型号",), scan=4):
    """在前 scan 行里找表头行：返回 (行号0基, {列名:列号0基})。

    表头格里为空 或 是纯数字占位 的列，从紧下一行同列补名字。
    """
    for ri in range(min(scan, len(rows))):
        names = [s(x) for x in (rows[ri] or [])]
        if not all(n in names for n in need):
            continue
        nxt = rows[ri + 1] if ri + 1 < len(rows) else []
        col = {}
        for i, n in enumerate(names):
            if n is None or _is_num_label(n):
                v2 = s(nxt[i]) if i < len(nxt) else None
                if v2:
                    n = v2
            if n:
                col.setdefault(n, i)
        return ri, col
    raise RuntimeError("找不到表头行 need=%s" % (need,))


def rows_of(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


def sc(rows, i, r):
    return s(r[i]) if (i is not None and i < len(r)) else None


def nc(rows, i, r):
    return num(r[i]) if (i is not None and i < len(r)) else None


# ---------------------------------------------------------------- 清理明细

def parse_clean(ws, kind):
    """kind: 'l'=滞销  's'=样机。返回记录列表。"""
    rows = rows_of(ws)
    hi, col = header_map(rows, need=("型号", "品牌"))
    i_mo = col["型号"]
    i_q = col["滞销清理数量"] if kind == "l" else col["样机清理数量"]
    i_p = col["滞销清理价"] if kind == "l" else col["样机清理价"]
    i_rb = col["滞销出库奖励"] if kind == "l" else col.get("样机出库存奖励") or col.get("样机出库奖励")
    i_av = col.get("可用量") if kind == "l" else col.get("现存量")
    print("  [%s] 表头行 r%d，列: %s" % (kind, hi + 1, {k: v for k, v in col.items() if v is not None}))
    out = []
    for r in rows[hi + 1:]:
        if not r:
            continue
        mo = sc(rows, i_mo, r)
        if not mo or norm_model(mo) == "型号":
            continue
        q, p = nc(rows, i_q, r), nc(rows, i_p, r)
        if not s(r[i_q] if i_q < len(r) else None) and not s(r[i_p] if i_p < len(r) else None):
            continue
        nat_raw, nat_k = nat_kind(sc(rows, col.get("全国可发"), r))
        out.append({
            "kind": kind, "mo": mo,
            "b": sc(rows, col.get("品牌"), r),
            "cat": sc(rows, col.get("品类"), r),
            "code": sc(rows, col.get("YS物料编码"), r),
            "n": sc(rows, col.get("YS物料名称"), r),
            "lp": nc(rows, col.get("零售指导价"), r),
            "nat": nat_raw, "natk": nat_k,
            "q": q, "p": p, "rb": nc(rows, i_rb, r), "av": nc(rows, i_av, r),
            "st": sc(rows, col.get("产品状态"), r),
        })
    return out


# ---------------------------------------------------------------- 现存量

def parse_stock(ws):
    """现存量 → {core: {mo, n, cat, av, on, sp, gp, wh:{key:{w,t,av,on}}}}"""
    rows = rows_of(ws)
    hi, col = header_map(rows, need=("型号", "可用量"))
    agg = {}
    for r in rows[hi + 1:]:
        if not r:
            continue
        mo = sc(rows, col["型号"], r)
        if not mo or norm_model(mo) == "型号":
            continue
        ck = core_of(mo)
        a = agg.setdefault(ck, {
            "mo": mo, "n": sc(rows, col.get("物料名称"), r),
            "cat": sc(rows, col.get("物料分类"), r),
            "av": 0, "on": 0, "sp": 0, "gp": 0, "wh": {}})
        av = nc(rows, col["可用量"], r)
        on = nc(rows, col.get("现存量"), r)
        sp = nc(rows, col.get("样机现存量"), r)
        gp = nc(rows, col.get("正品现存量"), r)
        for key, val in (("av", av), ("on", on), ("sp", sp), ("gp", gp)):
            if isinstance(val, (int, float)):
                a[key] += val
        w = sc(rows, col.get("仓库"), r)
        if w:
            k2 = "%s|%s" % (w, sc(rows, col.get("仓库类型"), r) or "")
            d = a["wh"].setdefault(k2, {"w": w, "t": sc(rows, col.get("仓库类型"), r), "av": 0, "on": 0})
            if isinstance(av, (int, float)):
                d["av"] += av
            if isinstance(on, (int, float)):
                d["on"] += on
    return agg


# ---------------------------------------------------------------- 主流程

def find_sheet(names, pred):
    for n in names:
        if pred(n):
            return n
    return None


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else None
    if not src:
        cand = sorted(glob.glob(r"D:\新建文件夹 (2)\公布版-滞销样机清理明细-*.xlsx"))
        if not cand:
            raise SystemExit("找不到在线表 xlsx")
        src = cand[-1]
    print("源文件:", src)
    wb = openpyxl.load_workbook(src, data_only=True, read_only=True)
    names = wb.sheetnames
    print("工作表:", names)

    nl = build.pick_sheet(names, "滞销清理明细")
    ns = build.pick_sheet(names, "样机清理明细")
    n10 = find_sheet(names, lambda n: n.startswith("现存量10"))
    nst = find_sheet(names, lambda n: n == "现存量")

    recl = parse_clean(wb[nl], "l") if nl else []
    recs = parse_clean(wb[ns], "s") if ns else []
    print("滞销清理 %d 条 / 样机清理 %d 条" % (len(recl), len(recs)))

    inv_a = parse_stock(wb[n10]) if n10 else {}
    inv_b = parse_stock(wb[nst]) if nst else {}
    print("库存 A <%s>: %d 个型号" % (n10, len(inv_a)))
    print("库存 B <%s>: %d 个型号" % (nst, len(inv_b)))

    # 合并 滞销 + 样机
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

    # 库存：先 A(10.x 快照) 后 B 补缺；B 更全时整表优先
    cov_a = sum(1 for ck in rows if ck in inv_a)
    cov_b = sum(1 for ck in rows if ck in inv_b)
    print("库存覆盖清理清单：A %d / B %d" % (cov_a, cov_b))
    primary, second = (inv_a, inv_b) if cov_a >= cov_b else (inv_b, inv_a)
    merged = dict(second)
    merged.update(primary)
    inv = merged

    n_inv = 0
    for ck, r in rows.items():
        iv = inv.get(ck)
        if not iv:
            continue
        r["inv"] = {"av": iv["av"], "on": iv["on"], "sp": iv["sp"], "gp": iv["gp"],
                    "wh": sorted(iv["wh"].values(), key=lambda x: -x["av"])[:12]}
        n_inv += 1
        if not r.get("n") and iv.get("n"):
            r["n"] = iv["n"]
        if not r.get("cat") and iv.get("cat"):
            r["cat"] = iv["cat"]

    # 图库
    imgp = os.path.join(HERE, "images_cache.json")
    allimg = json.load(io.open(imgp, encoding="utf-8")) if os.path.isfile(imgp) else {}
    imgs = {ck: v for ck, v in allimg.items() if ck in rows and isinstance(v, str)
            and v.startswith("data:image/")}

    m = re.search(r"(\d{4})\D+(\d{1,2})\D", os.path.basename(src))
    month = "%s-%02d" % (m.group(1), int(m.group(2))) if m else ""

    payload = {
        "meta": {
            "month": month,
            "src": os.path.basename(src),
            "built": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "online": {"share": "https://www.kdocs.cn/l/ch7gwGTP9De4", "fileId": "548020345856"},
        },
        "rows": sorted(rows.values(), key=lambda r: (r.get("b") or "", r.get("cat") or "", r["mo"])),
        "imgs": imgs,
    }
    with io.open(os.path.join(HERE, "_payload_kdocs.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    # ---- 统计
    bc = Counter(r.get("b") or "?" for r in payload["rows"])
    cc = Counter(r.get("cat") or "?" for r in payload["rows"])
    print("-" * 60)
    print("合并后型号 %d 个；有图 %d 个 (%.1f%%)" % (len(rows), len(imgs), 100.0 * len(imgs) / max(len(rows), 1)))
    print("品牌:", dict(bc.most_common()))
    print("品类(top12):", dict(cc.most_common(12)))
    print("样式: 仅滞销 %d / 仅样机 %d / 两者都有 %d" % (
        sum(1 for r in rows.values() if r["k"] == "l"),
        sum(1 for r in rows.values() if r["k"] == "s"),
        sum(1 for r in rows.values() if r["k"] == "ls")))
    print("带库存信息 %d 个" % n_inv)
    nat = Counter(str(r.get("nat")) for r in rows.values())
    print("全国可发取值:", dict(nat.most_common(8)))
    lpz = [r.get("lpz") for r in rows.values() if isinstance(r.get("lpz"), (int, float))]
    spz = [r.get("spz") for r in rows.values() if isinstance(r.get("spz"), (int, float))]
    if lpz:
        print("滞销清理价 有值 %d 个, %.0f~%.0f" % (len(lpz), min(lpz), max(lpz)))
    if spz:
        print("样机清理价 有值 %d 个, %.0f~%.0f" % (len(spz), min(spz), max(spz)))
    print("payload → _payload_kdocs.json")


if __name__ == "__main__":
    main()
