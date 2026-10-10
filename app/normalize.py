# -*- coding: utf-8 -*-
"""型号/品牌归一化（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：config → paths → state → normalize（本模块只依赖标准库）。

⚠ 口径三份手抄（改这里必须同步另两份）：
   ① 本文件（exe 端） ② parse_kdocs.py（离线单文件构建） ③ .github/scripts/yy_cloud_pull.py（CI 端）
"""
import re


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
