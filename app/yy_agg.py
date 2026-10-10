# -*- coding: utf-8 -*-
"""用友库存聚合口径（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
⚠ 口径文件：聚合三级候选 / 口径A+ / 裁判集（型号表当裁判）都在这里，
  与 CI 端 .github/scripts/yy_cloud_pull.py 的 aggregate 是**手抄镜像**——
  改此文件必须同步改 CI 端，两边口径才不劈叉。
依赖方向：yyclient 内部（agg 只被 updater 编排调用）。"""
import io
import json
import time

import state
from cloud import stock_push_cloud
from config import (YY_K_AVAIL, YY_K_MODEL, YY_K_QTY, YY_K_SKUNAME,
                    YY_K_WH, YY_K_WHTYPE)
from normalize import core_of, norm_model
from payload import get_payload
from paths import wp


def yy_finish(recs, log):
    """聚合 + 命中统计 + 落盘。"""
    # 原始行样本落盘（2026-10-10 排查字段用）：前 3 行 + 含「13TE7」的行，供核对接口字段真值
    try:
        samp = recs[:3] + [r for r in recs if "13TE7" in json.dumps(r, ensure_ascii=False).upper()][:3]
        n_empty_sku = sum(1 for r in recs if not str(r.get(YY_K_SKUNAME) or "").strip())
        n_splice = 0
        for r in recs:
            _mo = norm_model(r.get(YY_K_MODEL))
            _sku = norm_model(r.get(YY_K_SKUNAME))
            if _sku and _mo and _mo in _sku and _sku.index(_mo) + len(_mo) < len(_sku):
                n_splice += 1
        with io.open(wp("yy_last_rows.json"), "w", encoding="utf-8") as f:
            json.dump({"ts": time.strftime("%m-%d %H:%M:%S"), "rows": len(recs),
                       "empty_skuName": n_empty_sku, "spliceable": n_splice,
                       "sample": samp}, f, ensure_ascii=False, indent=1)
        log("已存原始行样本 %d 条（skuName 空 %d / 可拼接 %d）" % (len(samp), n_empty_sku, n_splice))
    except Exception as e:
        log("样本落盘失败：" + str(e))
    # 按型号聚合（norm_model 与前端 norm() 严格一致，全角/空格/大小写都归一）
    # 聚合键（2026-10-10 晚二次定稿「型号表当裁判」）：ERP「型号」字段本身是干净的
    # （BC/BD-629GHEP），脏尾缀全在物料SKU名称里（…大富豪系列）。但也不能无脑不拼——
    #   · 型号字段常被录成截短值（JSQ25-13TE7，完整 JSQ25-13TE7(12T)星河U1 在名称里）；
    #   · 还有的型号中文尾就是本体（BCD-267WKR3NPGA星光棕 / XX套机），全靠拼接才对得上。
    # 所以：拼接键在型号表里 → 用拼接键；干净型号在表里、拼接键不在 → 剥掉描述尾缀用
    # 干净键；都不在 → 维持拼接（宁可键脏也别丢数量，前端还有尾缀兜底匹配）。
    # whs = 每个仓的明细（2026-10 二次改版：卡片只显示「什么仓有几个」，不再显示可用/现存总数）
    T_MO = set()
    try:
        for _r in (get_payload().get("rows") or []):
            _m = norm_model(_r.get("mo"))
            if _m:
                T_MO.add(_m)
    except Exception:
        pass                              # 表拿不到就退回纯拼接口径，不影响出数
    agg = {}
    agg_code = {}                          # 编码桶（2026-10-10 深夜「编码优先」）：与型号桶并行
    for r0 in recs:
        mo = norm_model(r0.get(YY_K_MODEL))
        if not mo:
            continue
        sku = norm_model(r0.get(YY_K_SKUNAME))
        full = mo
        if sku and mo in sku:
            tail = sku[sku.index(mo) + len(mo):]
            if tail:
                full = mo + tail          # 完整名候选：型号字段截短时靠它一字不差对上
        # 三级候选按序取第一个在型号表里的（与 yy_cloud_pull.py aggregate 同款）：
        # ① 拼接键（截短补全）②型号字段原样（skuName 带描述尾）③型号字段剥中文尾
        #   （型号字段自身带描述尾，BC/BD-100GHPG9Z小富豪；core_of 与前端 coreOf 同款）
        key = full
        if T_MO:
            if full in T_MO:
                key = full
            elif mo in T_MO:
                key = mo
            else:
                mo_c = core_of(r0.get(YY_K_MODEL))
                if mo_c and mo_c in T_MO:
                    key = mo_c
        a = agg.setdefault(key, {"q": 0.0, "a": 0.0, "wt": {}, "whs": {}})
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
        # 编码维度并行聚一份（同一行既进型号桶也进编码桶；productsku_cCode=YS物料编码同源）
        code = norm_model(r0.get("productsku_cCode"))
        if code and not code.startswith("#"):          # 排除 #N/A 脏值
            ac = agg_code.setdefault(code, {"q": 0.0, "a": 0.0, "wt": {}, "whs": {}})
            try:
                ac["q"] += float(r0.get(YY_K_QTY) or 0)
            except (TypeError, ValueError):
                pass
            ac["a"] += av
            ac["wt"][wt] = round(ac["wt"].get(wt, 0.0) + av, 2)
            ac["whs"][k2] = round(ac["whs"].get(k2, 0.0) + av, 2)

    # whs: {(仓名, 类型): 数量} → 排序列表 [{w, t, n}]（数量多的在前）
    for a in agg.values():
        lst = [{"w": k[0], "t": k[1], "n": v} for k, v in a["whs"].items()]
        lst.sort(key=lambda x: (-x["n"], x["w"]))
        a["whs"] = lst
    for a in agg_code.values():
        lst = [{"w": k[0], "t": k[1], "n": v} for k, v in a["whs"].items()]
        lst.sort(key=lambda x: (-x["n"], x["w"]))
        a["whs"] = lst

    # 命中统计：跟当前表里的型号对一下
    hit = 0
    for _m in T_MO:
        if _m in agg:
            hit += 1

    ts = time.strftime("%m-%d %H:%M")
    with state.STOCK_LOCK:
        state.STOCK = {"ts": ts, "rows": len(recs), "stock": agg, "sbc": agg_code}
    state.stock_save()
    log("实时库存更新完成：%d 行 · %d 个型号 · 命中表中 %d 个型号" % (len(recs), len(agg), hit))
    state._STK_DETAIL[0] = "%d 行现存量 · %d 个型号有货 · 命中表中 %d 个，已共享云端" % (len(recs), len(agg), hit)
    state.set_state("done", "实时库存已更新：%d 行 · 命中表中 %d 个型号（%s）" % (len(recs), hit, ts),
                    stock_at=ts)
    stock_push_cloud(log)          # 管理号拉完即上云，子账号全员自动跟进（2026-10-08）
