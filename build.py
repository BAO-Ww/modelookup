# -*- coding: utf-8 -*-
"""
隆阳电器 · 型号查询 —— 构建脚本
把每月一份的「价格表 xlsx」解析清洗后，生成单文件离线 HTML 查询工具。

用法：
    python build.py "D:\\path\\2026年10月价格表-LYDF-共享.xlsx"
    python build.py <xlsx> -o 输出.html

设计要点（针对源表已知的坑）：
  1) 表头行不固定在第一行 —— 自动搜索含「型号」的那一行作为表头行，
     表头之前的行（说明文字 + T1/Y1 参数格）一律丢弃。
  2) 美的表是两段拼接（美的 + 容声），中间夹了一段重复表头 ——
     遍历时凡 F 列值等于「型号」的行一律跳过。
  3) 公式列（库存 SUMIFS、各种毛利）读取 xlsx 缓存值，不做重算，
     保证与用户在 Excel 里看到的数字一致。
  4) 型号为空的幽灵行全部丢弃。
"""
import argparse
import datetime as _dt
import json
import os
import re
import sys

import openpyxl

# ---------------------------------------------------------------- 工具函数


def s(v):
    """单元格 → 去空白字符串，空返回 None。"""
    if v is None:
        return None
    t = str(v).strip()
    return t if t else None


def num(v):
    """单元格 → 数字；无法转换时原样返回文本（如「无」）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    t = str(v).strip().replace(",", "")
    if t == "":
        return None
    try:
        f = float(t)
        return int(f) if f.is_integer() and abs(f) < 1e15 else f
    except ValueError:
        return t


def pick_sheet(names, *keys):
    """按关键词模糊匹配 sheet 名（月表名字会变，如「现存量10.6」）。"""
    for k in keys:
        for n in names:
            if k in n:
                return n
    return None


def find_header_row(ws, max_scan=8):
    """在 F 列里找值为「型号」的那一行。"""
    for r in range(1, min(ws.max_row, max_scan) + 1):
        for c in range(1, min(ws.max_column, 12) + 1):
            v = ws.cell(row=r, column=c).value
            if v is not None and str(v).strip() == "型号":
                return r
    return None


def row_vals(ws, r, ncol=26):
    return [ws.cell(row=r, column=c).value for c in range(1, ncol + 1)]


def g(row, i):
    """1-based 取值。"""
    return row[i - 1] if 0 <= i - 1 < len(row) else None


# ---------------------------------------------------------------- 解析


def parse_haier(ws):
    """源表 A：型号价格表（只看这个）"""
    hr = find_header_row(ws)
    if hr is None:
        raise RuntimeError("未能在「型号价格表」里定位表头行")
    out = []
    for r in range(hr + 1, ws.max_row + 1):
        row = row_vals(ws, r, 26)
        model = s(g(row, 6))
        if not model or model == "型号":
            continue
        rec = {
            "c": s(g(row, 2)),            # 品类
            "b": s(g(row, 3)),            # 品牌
            "code": s(g(row, 4)),         # 编码
            "n": s(g(row, 5)),            # 名称
            "mo": model,                  # 型号
            "st": s(g(row, 7)),           # 产品状态
            "p0": num(g(row, 8)),         # 裸价
            "mg": num(g(row, 9)),         # 裸价批发价点位（毛利率）
            "p1": num(g(row, 10)),        # 零售指导价
            "p2": num(g(row, 11)),        # 价格 含税
            "p3": num(g(row, 12)),        # 滞销价格
            "p4": num(g(row, 13)),        # 样机价格
            "ar": s(g(row, 14)),          # 发货区域
            "inv": num(g(row, 15)),       # 正品可用库存
            "invs": num(g(row, 16)),      # 样机可用库存
            "src": s(g(row, 17)),         # 货来源
            "rmk": s(g(row, 18)),         # 备注
            "xyid": s(g(row, 19)),        # 闲鱼商品ID
            "xyp": num(g(row, 20)),       # 闲鱼价格
            "xypr": num(g(row, 21)),      # 闲鱼利润
            "sc": s(g(row, 23)),          # 补贴69码
            "sp1": num(g(row, 24)),       # 补贴最高价格
            "sp2": num(g(row, 25)),       # 补贴可赚
            "sp3": num(g(row, 26)),       # 用户补贴金额
        }
        out.append({k: v for k, v in rec.items() if v is not None})
    return out, hr


def parse_midea(ws):
    """源表 B：美的（含容声段，中间有重复表头）"""
    hr = find_header_row(ws)
    if hr is None:
        raise RuntimeError("未能在「美的」表里定位表头行")
    out = []
    for r in range(hr + 1, ws.max_row + 1):
        row = row_vals(ws, r, 19)
        model = s(g(row, 6))
        if not model or model == "型号":
            continue
        if s(g(row, 3)) == "品牌":          # 残片行
            continue
        rec = {
            "cat": s(g(row, 2)),          # 大品类
            "brand": s(g(row, 3)),        # 品牌
            "name": s(g(row, 5)),         # 名称
            "mo": model,                  # 型号
            "gp": num(g(row, 7)),         # 拿货价（慧买卖）
            "ph": num(g(row, 8)),         # 价格（给二批）
            "pi": num(g(row, 9)),         # 价格取整
            "pj": num(g(row, 10)),        # 零售指导价
            "mk": num(g(row, 11)),        # 零售毛利
            "pl": num(g(row, 12)),        # 京东价或国补后价
            "mm": num(g(row, 13)),        # 京东国补后毛利
            "rmk": s(g(row, 14)),         # 备注
            "o": num(g(row, 15)),         # 开票2%
            "op": num(g(row, 16)),        # 开票2% 剩余
            "q": num(g(row, 17)),         # 价格-拿货-开票2%=剩余
            "xy": num(g(row, 18)),        # 闲鱼
        }
        out.append({k: v for k, v in rec.items() if v is not None})
    return out, hr


def parse_stock(ws):
    """源表 C：现存量 —— 按型号聚合仓库分布（辅助校验库存）。"""
    hr = find_header_row(ws)
    if hr is None:
        raise RuntimeError("未能在「现存量」表里定位表头行")
    idx = {}
    for r in range(hr + 1, ws.max_row + 1):
        row = row_vals(ws, r, 19)
        model = s(g(row, 6))
        if not model or model == "型号":
            continue
        wh = s(g(row, 2))            # 仓库
        wtype = s(g(row, 7))         # 仓库类型
        avail = num(g(row, 14))      # 可用量
        qty = num(g(row, 13))        # 现存量
        try:
            a = float(avail) if avail is not None else 0.0
        except (TypeError, ValueError):
            a = 0.0
        if a == 0:
            continue                 # 无库存的行不入明细，控制体积
        d = idx.setdefault(model, {"t": {}, "w": []})
        if wtype:
            d["t"][wtype] = d["t"].get(wtype, 0) + a
        d["w"].append([wh or "未知仓库", wtype or "", a, qty if isinstance(qty, (int, float)) else None])
    for m, d in idx.items():
        d["w"].sort(key=lambda x: -x[2])
        d["w"] = d["w"][:40]
        d["t"] = {k: (int(v) if float(v).is_integer() else round(v, 2)) for k, v in d["t"].items()}
    return idx, hr


# ---------------------------------------------------------------- 官网目录 / 型号图


def norm_model(v):
    """与前端 norm() 一致：全角→半角、去空白、大写。"""
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
    """与前端 coreOf() 一致：去掉首部中文及之后（颜色/套机等后缀）。

    截断后还要剃掉尾部残留的非型号字符，例如
    「BCD-550WGCFDM4WHU1(棕)」→ 截掉「棕」后剩「...WHU1(」→ 再 rstrip 成「...WHU1」。
    """
    n = norm_model(v)
    m = re.search(r"[\u4e00-\u9fff]", n)
    if m and m.start() >= 3:
        return n[: m.start()].rstrip("(-_/+.,、，")
    return n


def load_official(here, haier, midea):
    """官网目录 catalog.json + 图片缓存 images_cache.json → payload 的 cat / cx / imgs。

    cat: 表内型号命中的官网信息 {core: [品牌, 品类, 名称, 官网URL]}（卡片上配图/官网按钮用）
    cx : 官网库独有型号列表 [{mo, b, cat, n, u}]（价格表暂无的官网在售型号）
    imgs: {core: 缩略图 dataURL}
    """
    cat_path = os.path.join(here, "catalog.json")
    imgs_path = os.path.join(here, "images_cache.json")
    payload = {}
    if not os.path.isfile(cat_path):
        return payload, 0, 0
    with open(cat_path, encoding="utf-8") as f:
        catalog = json.load(f)

    tcores = set()
    for r in haier:
        tcores.add(core_of(r["mo"]))
    for r in midea:
        tcores.add(core_of(r["mo"]))

    # 只保留「表格里有的型号」：官网目录仅用来给表内型号补 品牌/品类/名称/官网链接，
    # 官网独有的型号（cx）一律不进 payload —— 用户明确要求页面只显示自己表格里的型号。
    cat_hit, cx = {}, []
    for core, v in catalog.items():
        if core in tcores:
            cat_hit[core] = [v.get("b"), v.get("cat"), v.get("n"), v.get("u")]
    payload["cat"] = cat_hit
    payload["cx"] = cx

    if os.path.isfile(imgs_path):
        with open(imgs_path, encoding="utf-8") as f:
            all_imgs = json.load(f)
        # 配图同样只保留能对上表格型号的
        payload["imgs"] = {k: v for k, v in all_imgs.items()
                           if k in tcores and isinstance(v, str) and v.startswith("data:image/")}

    # 用户自己补的图（页面里「导出我补的图」得到 补图.json，丢到本目录即命名 manual_imgs.json）
    # 优先级最高，直接覆盖同名型号的自动配图。
    man_path = os.path.join(here, "manual_imgs.json")
    if os.path.isfile(man_path):
        with open(man_path, encoding="utf-8") as f:
            man = json.load(f)
        n_man = 0
        for k, v in man.items():
            if k in tcores and isinstance(v, str) and v.startswith("data:image/"):
                payload["imgs"][k] = v
                n_man += 1
        print("手动补图：并入 %d 张" % n_man)
    return payload, len(cat_hit), len(cx)


# ---------------------------------------------------------------- 主流程


def build(src, out_path, month=None):
    if not os.path.isfile(src):
        raise SystemExit("找不到源文件：%s" % src)

    # 注意：不能用 read_only=True —— 该模式下 Worksheet.cell() 会退化为
    # 每次重新遍历整表（O(n^2)），8000 行的现存量表会把进程拖死。
    wb = openpyxl.load_workbook(src, data_only=True)
    names = wb.sheetnames

    n_h = pick_sheet(names, "型号价格表", "只看这个")
    n_m = pick_sheet(names, "美的")
    n_s = pick_sheet(names, "现存量")
    if not n_h:
        raise SystemExit("没找到「型号价格表」sheet，实际 sheet：%s" % names)

    haier, hr_h = parse_haier(wb[n_h])
    midea, hr_m = ([], None)
    if n_m:
        midea, hr_m = parse_midea(wb[n_m])
    stock, hr_s = ({}, None)
    if n_s:
        stock, hr_s = parse_stock(wb[n_s])

    if not month:
        m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月", os.path.basename(src))
        if m:
            month = "%s-%02d" % (m.group(1), int(m.group(2)))
        else:
            month = _dt.datetime.fromtimestamp(os.path.getmtime(src)).strftime("%Y-%m")

    meta = {
        "month": month,
        "built": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": os.path.basename(src),
        "sheets": {"price": n_h, "midea": n_m, "stock": n_s},
        "count_h": len(haier),
        "count_m": len(midea),
        "count_stock_models": len(stock),
    }

    here = os.path.dirname(os.path.abspath(__file__))
    extra, n_hit, n_cx = load_official(here, haier, midea)
    meta["cat_hit"] = n_hit
    meta["cat_cx"] = n_cx
    meta["cat_imgs"] = len(extra.get("imgs") or {})

    payload = {"meta": meta, "h": haier, "m": midea, "s": stock}
    payload.update(extra)

    data_js = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    data_js = data_js.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")

    tpl_path = os.path.join(here, "template.html")
    with open(tpl_path, "r", encoding="utf-8") as f:
        tpl = f.read()
    html = tpl.replace("/*__DATA__*/", data_js)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    size = os.path.getsize(out_path) / 1024.0
    print("[ok] %s" % out_path)
    print("     海尔系 %d 条 / 美的系 %d 条 / 有库存型号 %d 个" % (len(haier), len(midea), len(stock)))
    print("     官网目录：表内命中 %d 款（官网独有型号已按需求剔除） / 配图 %d 张" % (n_hit, meta["cat_imgs"]))
    print("     表头行 -> 价格表:%s 美的:%s 现存量:%s" % (hr_h, hr_m, hr_s))
    print("     文件大小 %.0f KB" % size)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="把月度价格表 xlsx 构建成单文件型号查询 HTML")
    ap.add_argument("src", help="源 xlsx 路径")
    ap.add_argument("-o", "--out", default=None, help="输出 html 路径")
    ap.add_argument("-m", "--month", default=None, help="数据月份，如 2026-10")
    a = ap.parse_args()
    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.src)), "型号查询.html")

    here = os.path.dirname(os.path.abspath(__file__))
    logp = os.path.join(here, "build.log")
    import io
    import traceback
    buf = io.StringIO()
    old, sys.stdout = sys.stdout, buf
    try:
        build(a.src, out, a.month)
    except Exception:
        print(traceback.format_exc())
    finally:
        sys.stdout = old
    text = buf.getvalue()
    with open(logp, "w", encoding="utf-8") as f:
        f.write("src = %s\nout = %s\n\n%s" % (a.src, out, text))
    print(text)


if __name__ == "__main__":
    main()
