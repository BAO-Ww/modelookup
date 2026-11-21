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

# 仓库黑名单：
#   · 「东莞事业部统仓统配（东莞）ZP仓」不要、也不算进库存（2026-11-05 用户令）；
#   · 「内部福利仓」不要、也不算进库存（2026-11-16 用户令「内部福利仓的全部也不要
#     纳入有库存的」）——福利仓的货不是可售货，只挂福利仓的型号应当算无货。
#   子串匹配（仓名里出现即整行剔除），故意不用「ZP仓」做关键词——
#   「分销统仓统配ZP仓」是另一个仓，用户没说不要，不能误伤。
#   ⚠ 手抄镜像：.github/scripts/yy_cloud_pull.py 的 aggregate 必须同步同款，
#   两边口径劈叉 = 管理号本机拉的与 CI 云拉的库存对不上。
# 仓库计入口径（2026-11-20 用户令，来源《仓库26.11.19.xlsx》）：
# 用友 248 个仓库里 152 个在「仓库库存计入」列标注「不计」——机壳/样机/礼品/次品/
# 虚拟/固定资产/外采过渡等仓的货一律不算有库存。精确名单匹配（WH_EXCLUDE_NAMES），
# 不用子串——避免误伤「分销统仓统配ZP仓」这类仍在计口径的仓；原有两条子串规则保留兜底。
# ⚠ 手抄镜像：app/yy_agg.py 与 .github/scripts/yy_cloud_pull.py 的 WH_EXCLUDE_NAMES
#   必须同步同款，两边口径劈叉 = 管理号本机拉的与 CI 云拉的库存对不上。
WH_EXCLUDE = (
    "东莞事业部统仓统配",
    "内部福利仓",
)
WH_EXCLUDE_NAMES = frozenset((
    "城市体验中心ZP仓",
    "海尔坪山CC仓",
    "龙华无印仓",
    "JC-C库",
    "海尔坪山外采ZP仓",
    "海尔坪山LP仓",
    "同孚卫浴仓【代管】",
    "海尔东莞统仓统配ZP仓",
    "龙华观澜皇家一号JK仓",
    "延保卡仓",
    "公司LP仓",
    "固定资产仓",
    "平湖香江家居JK仓",
    "城市体验中心JK仓",
    "海尔统仓统配CC仓",
    "东莞事业部坪山ZP仓",
    "东莞事业部统仓统配（东莞）ZP仓",
    "隆顺坪山JK仓",
    "东莞凤岗国安居YJ仓",
    "惠州大亚湾亲水湾YJ仓",
    "东莞大朗光辉YJ仓",
    "东莞大朗光辉JK仓",
    "东莞凤岗国安居JK仓",
    "惠州大亚湾居然之家JK仓",
    "惠州大亚湾居然之家YJ仓",
    "惠州大亚湾亲水湾JK仓",
    "惠州惠城国安居YJ仓",
    "惠州惠阳国安居JK仓",
    "惠州惠阳国安居YJ仓",
    "欧洲城香江家居JK仓",
    "福永创思家居JK仓",
    "坪山定制家居仓",
    "东莞塘厦光辉YJ仓",
    "龙岗布吉甘坑YJ仓",
    "电商事业部LP仓",
    "光明辉煌万家JK仓",
    "海尔统仓统配JK仓",
    "坪山乐安居JK仓",
    "东莞事业部东莞ZP仓",
    "惠州惠城国安居JK仓",
    "线上采购过渡ZP仓",
    "东莞樟木头红树湾JK仓",
    "东莞塘厦光辉JK仓",
    "惠州大亚湾亲水湾LP仓",
    "惠州惠城国安居LP仓",
    "东莞事业部DSYJ仓",
    "内部福利仓",
    "材料仓",
    "民治国安居JK仓",
    "福永创思家居LP仓",
    "平湖香江家居LP仓",
    "光明鸿发居JK仓",
    "龙岗布吉甘坑JK仓",
    "龙岗布吉华晖LP仓",
    "隆阳费用仓",
    "龙华美团ZP仓",
    "龙岗布吉甘坑LP仓",
    "华润万家中六YJ仓",
    "华润万家龙洞YJ仓",
    "惠州惠阳国安居LP仓",
    "光明辉煌万家LP仓",
    "光明鸿发居LP仓",
    "工程安装一部材料仓",
    "隆阳虚拟过渡仓",
    "基地锁货仓",
    "工程部坪山外采ZP仓",
    "华润万家荔湾YJ仓",
    "紫和装饰YJ仓",
    "金龙华广场天猫门店样机仓",
    "金龙华广场天猫门店正品仓",
    "观澜正品仓",
    "优悠家天猫云仓",
    "优悠家天猫寄存仓",
    "外采临时过渡仓",
    "东莞事业部爆品ZP仓",
    "二十五号惠阳仓",
    "永超坪山ZP仓",
    "二十五号线下仓",
    "龙岗布吉华晖ZP仓",
    "物流回收瑕疵品仓",
    "城市体验中心LP仓",
    "深圳华强北京东MALL",
    "汇优惠阳仓",
    "翠竹苏宁易购直营店样机仓库",
    "龙岗苏宁易购直营店样机仓库",
    "深圳南山京东MALL",
    "东莞京东超体",
    "工厂不良品库",
    "汕头京东超体",
    "冯所长样机库",
    "东莞事业部融资ZP仓",
    "深圳西乡圣淘沙苏宁直营店样机仓",
    "东莞万达广场苏宁直营店样机仓",
    "汕头苏宁广场直营店样机仓",
    "电商事业部DSYJ仓",
    "二十五号商贸固定资产仓库",
    "25号不良品仓",
    "华润万家春风JK仓",
    "华润万家莲塘JK仓",
    "华润万家益田JK仓",
    "华润万家翠竹YJ仓",
    "龙岗世贸",
    "海雅缤纷城样机库",
    "惠州京东城旗",
    "华润万家龙岗YJ仓",
    "华润万家沙井YJ仓",
    "龙华万众城家居JK仓",
    "日立坪山CC仓",
    "隆阳-华润万家西丽店YJ仓",
    "龙华京东云鲸门店样机库",
    "隆顺日立共享ZP仓",
    "安得武汉东西湖ZP仓",
    "工程深圳云仓",
    "华润万家龙华YJ仓",
    "坪山电商ZP仓",
    "追觅门店样机汇总",
    "隆顺坪山外采ZP仓",
    "龙华整装馆样机仓",
    "电商坪山CC仓",
    "隆顺事业部虚拟过渡仓",
    "万饰城日立YJ仓",
    "海尔外采过渡ZP仓",
    "海尔工程异地配送ZP仓",
    "龙岗布吉华晖JK仓",
    "惠州大亚湾居然之家LP仓",
    "坪山海粤LP仓",
    "隆顺坪山LP仓",
    "工程安装二部材料仓",
    "星云创样机库",
    "坪山乐安居LP仓",
    "东莞海粤皓群ZP仓",
    "供应商代发ZP仓",
    "隆顺坪山CC仓",
    "厂家寄存仓",
    "永超坪山YJ仓",
    "永超自提仓",
    "东莞事业部CC仓",
    "华润万家五羊店YJ仓",
    "华润万家莲塘LP仓",
    "日立龙华金龙华LP仓",
    "云鲸样机库",
    "东莞事业部统仓统配（深圳）ZP仓",
    "华强北电子世界样机仓",
    "沙井京东样机仓",
    "时尚东城店样机",
    "时尚莞城店样机",
    "时尚虎门店样机",
    "海尔坪山待退厂仓",
    "长安镇美汇家具苏宁(下沉门店）",
    "华润万家大鹏JK仓",
    "海尔哪买哪送仓",
    "唯品分销ZP仓",))

def wh_excluded(wname):
    s = str(wname or "").strip()
    if s in WH_EXCLUDE_NAMES:
        return True
    for x in WH_EXCLUDE:
        if x in s:
            return True
    return False



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
    mo2meta = {}                           # 型号→编码/物料名称（2026-11-06 用户令「物料编码没读取到，
                                           # 读取到再加上去」：金山清理明细的 YS物料编码 本身可能是
                                           # VLOOKUP #N/A（JSQ30-16WM7DPTGU1 实锤），用友行里有真
                                           # 编码跟型号同行 —— 聚一份映射给 payload 重建时补缺）
    for r0 in recs:
        # 2026-11-05 仓库黑名单：整行剔除（型号桶/编码桶都不进，q/a/wt/whs 全干净）
        if wh_excluded(r0.get(YY_K_WH)):
            continue
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
        # 2026-11-21 用户令：皓群货盘的价格跟「组织」走（组织=广州皓群电器有限公司的仓才算
        # 皓群的货）——组织名随仓一起进 whs 明细（键带 org，条目落 "o" 字段）。
        # 实锤：皓群主仓「广州统仓统配ZP仓」仓名不含皓群；「东莞海粤皓群ZP仓」挂海粤组织。
        # ⚠ 手抄镜像：.github/scripts/yy_cloud_pull.py 的 aggregate 必须同步同款。
        org = str(r0.get("org_name") or "").strip()
        k2 = (wname, wt, org)
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
        # 型号→编码/物料名称对码（同 key 多行取第一个非空干净值）
        _cc = str(r0.get("productsku_cCode") or "").strip()
        _sn = str(r0.get(YY_K_SKUNAME) or "").strip()
        if _cc or _sn:
            m2 = mo2meta.setdefault(key, {})
            if _cc and not _cc.startswith("#") and not m2.get("code"):
                m2["code"] = _cc
            if _sn and not _sn.startswith("#") and not m2.get("n"):
                m2["n"] = _sn

    # whs: {(仓名, 类型, 组织): 数量} → 排序列表 [{w, t, n, o}]（数量多的在前）
    for a in agg.values():
        lst = [{"w": k[0], "t": k[1], "n": v, "o": (k[2] if len(k) > 2 else "")}
               for k, v in a["whs"].items()]
        lst.sort(key=lambda x: (-x["n"], x["w"]))
        a["whs"] = lst
    for a in agg_code.values():
        lst = [{"w": k[0], "t": k[1], "n": v, "o": (k[2] if len(k) > 2 else "")}
               for k, v in a["whs"].items()]
        lst.sort(key=lambda x: (-x["n"], x["w"]))
        a["whs"] = lst

    # 命中统计：跟当前表里的型号对一下
    hit = 0
    for _m in T_MO:
        if _m in agg:
            hit += 1

    ts = time.strftime("%Y-%m-%d %H:%M")   # 2026-10-14 带年份（体检 P1-A1：旧 %m-%d 跨年字典序比较必炸）
    with state.STOCK_LOCK:
        state.STOCK = {"ts": ts, "rows": len(recs), "stock": agg, "sbc": agg_code,
                       "mo2meta": mo2meta or None}
    state.stock_save()
    log("实时库存更新完成：%d 行 · %d 个型号 · 命中表中 %d 个型号" % (len(recs), len(agg), hit))
    state._STK_DETAIL[0] = "%d 行现存量 · %d 个型号有货 · 命中表中 %d 个，已共享云端" % (len(recs), len(agg), hit)
    state.set_state("done", "实时库存已更新：%d 行 · 命中表中 %d 个型号（%s）" % (len(recs), hit, ts),
                    stock_at=ts)
    stock_push_cloud(log)          # 管理号拉完即上云，子账号全员自动跟进（2026-10-08）
