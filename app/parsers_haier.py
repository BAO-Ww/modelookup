# -*- coding: utf-8 -*-
"""海尔系两表解析（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
公布版清理明细（滞销 l / 样机 s）+ LYDF 价格表。
依赖方向：normalize → parsing（xlsx_core / parsers_haier / parsers_yr）。"""
from normalize import norm_model, nat_kind
from xlsx_core import col_of, header_map, nc, rows_of, sc


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
            # 2026-10-10 用户拍板：价格表只读「价格含税」；滞销价格/样机价格不要了
            # （滞销/样机一律只认「公布版-滞销样机清理明细」的清理价 lpz/spz）
            "pt": nc(col_of(col, "价格含税"), r),
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
                   ("pt", "s69", "xyp")):
            continue          # 这一行什么都没，跳过
        out.append(rec)
    return out
