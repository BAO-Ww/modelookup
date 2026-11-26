# -*- coding: utf-8 -*-
r"""聚光四张 · 闲鱼新样式（2026-11-26 用户令「把 haier_xianyu_kit 弄到聚光」）
====================================================================
版式来源 = 根目录 haier_xianyu_kit.py（用户跑出的四张样张拍板）：
米白底 + 红竖条标题栏 + 白卡片；首图胶囊沿用旧聚光黄底(255,199,0)白字
（2026-11-26 追加用户令「首图的字的颜色还是要按旧的颜色」）；
首图文字=旧聚光原文（2026-11-26 用户令「首图的字还是按原来的字，不能改」）：
胶囊 l1/l2/型号 + 底部 features_line，一律旧口径不新造文案。

职责边界：本模块只管画——数据（卖点/参数/尺寸/logo/机器图）全部由调用方
（pei_ai9 聚光分支）显式传入，不做任何抓取/读盘决策；gen_* 返回画好的
RGB Image，落盘必须走 pei_ai9.save()（聚光隐藏水印 stamp_save 链不能绕）。

四张输出文件名保持聚光旧名（1_首图/2_机器图/4_核心参数/5_尺寸图.jpg），
前端 SLAB 标签、单张重出槽位、对账链零改动。"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

W = H = 1080

BG     = (250, 248, 243)   # 米白背景
CARD   = (255, 255, 255)
RED    = (200, 16, 46)     # 主题红（标题竖条/编号/尺寸数字）
INK    = (44, 44, 44)
GRAY   = (112, 112, 112)
LIGHT  = (233, 229, 220)   # 卡片描边/分隔线
YELLOW = (255, 199, 0)     # 首图胶囊旧聚光黄（2026-11-26 用户令按旧色）
YELLOW_FG = (255, 255, 255)

if os.name == "nt":
    _FD = os.path.join(os.environ.get("WINDIR", "C:/Windows"), "Fonts")
    F_BOLD = os.path.join(_FD, "msyhbd.ttc")
    F_REG = os.path.join(_FD, "msyh.ttc")
else:   # macOS 同 kitset 口径：STHeiti 双文件最稳
    F_BOLD = "/System/Library/Fonts/STHeiti Medium.ttc"
    F_REG = "/System/Library/Fonts/STHeiti Light.ttc"

_fb, _fr = {}, {}


def B(sz):
    f = _fb.get(sz)
    if f is None:
        f = _fb[sz] = ImageFont.truetype(F_BOLD, sz)
    return f


def R(sz):
    f = _fr.get(sz)
    if f is None:
        f = _fr[sz] = ImageFont.truetype(F_REG, sz)
    return f


def _fit(d, txt, base, maxw, bold=True, floor=18):
    """单行文字超宽自动缩号（940px 安全线内）。"""
    sz = base
    fnt = B if bold else R
    while sz > floor and d.textlength(txt, font=fnt(sz)) > maxw:
        sz -= 2
    return sz


def canvas():
    im = Image.new("RGB", (W, H), BG)
    return im, ImageDraw.Draw(im)


def header(im, d, title, en, right, logo=None):
    """统一标题栏：左红竖线 + 中文大标题 + 英文/型号副题，右上 logo，右下角卖点。"""
    d.rounded_rectangle((40, 46, 47, 126), radius=3, fill=RED)
    _s = _fit(d, title, 58, 640)
    d.text((66, 42), title, font=B(_s), fill=INK)
    d.text((70, 116), en or "", font=R(18), fill=GRAY)
    if right:
        d.text((1040, 116), right, font=B(19), fill=RED, anchor="ra")
    if logo is not None:
        try:
            lw = 196
            lh = max(1, int(logo.height * lw / float(logo.width)))
            small = logo.resize((lw, lh), Image.LANCZOS)
            im.paste(small, (1040 - lw, 116 - lh), small)
        except Exception:
            pass
    d.line((30, 152, 1050, 152), fill=LIGHT, width=2)


def footer(d, brand=""):
    """底部红色引导条（仅尺寸图使用）；联保文案品牌词动态。"""
    d.rounded_rectangle((30, 912, 1050, 1002), radius=45, fill=RED)
    d.text((540, 957), "先告知收货省市，帮您查当地库存",
           font=B(33), fill=(255, 255, 255), anchor="mm")
    _b = (str(brand or "").strip() + "全国联保") if str(brand or "").strip() else "全国联保"
    d.text((540, 1035), "全新正品  ·  %s  ·  支持到货验货" % _b,
           font=R(19), fill=GRAY, anchor="mm")


def pill_yellow(im, d, cx, cy, text, sz):
    """首图胶囊：旧聚光黄底白字（用户令按旧色），宽高全跟文字走，超宽自动缩号。"""
    while sz > 24 and d.textlength(text, font=B(sz)) + sz * 2.4 > 960:
        sz -= 2
    tw = d.textlength(text, font=B(sz))
    ph = int(sz * 2.0)
    pw = int(tw + sz * 1.7)
    d.rounded_rectangle((cx - pw / 2, cy - ph / 2, cx + pw / 2, cy + ph / 2),
                        radius=ph // 2, fill=YELLOW)
    d.text((cx, cy + 2), text, font=B(sz), fill=YELLOW_FG, anchor="mm")


def product_tight(prod, max_w, max_h):
    """机器图裁掉透明边后等比缩放，让机器本体最大化（抠底 PNG 用 alpha bbox 最准）。"""
    p = prod.convert("RGBA")
    try:
        bb = p.getchannel("A").getbbox()
        if bb:
            p = p.crop(bb)
    except Exception:
        pass
    w, h = p.size
    sc = min(max_w / float(max(1, w)), max_h / float(max(1, h)))
    return p.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.LANCZOS)


def _paste_center(im, p, cx, cy):
    im.paste(p, (int(cx - p.width / 2.0), int(cy - p.height / 2.0)), p)


# ---------------------------------------------------------------- 四张
def gen_cover(prod, l1, l2, model, brand, cap, logo, feat="", cat=""):
    """首图：标题栏 + 白卡机器大图 + 三颗黄胶囊（旧的黄底白字）。
    胶囊文字=旧聚光原文（2026-11-26 用户令「首图的字还是按原来的字，不能改」）：
    第一颗 l1（海尔波轮洗衣机）、第二颗 l2（12公斤）、第三颗型号——与旧聚光
    首图逐字一致，不新造文案；底部一行 features_line（旧口径
    cfg['features_line']，如「12公斤大容量 · 变频 · 一级」），没有就不画。"""
    im, d = canvas()
    _title = str(l1 or "").strip() or ((str(brand or "").strip() + str(cat or "").strip()) or model)
    header(im, d, _title, "%s · %s" % (model, brand) if brand else model,
           cap or "", logo)
    d.rounded_rectangle((30, 168, 1050, 1020), radius=20,
                        fill=CARD, outline=LIGHT, width=2)
    if prod is not None:
        try:
            _paste_center(im, product_tight(prod, 760, 800), 540, 600)
        except Exception:
            pass
    caps = []
    for t in (str(l1 or "").strip() or str(cap or "").strip(),
              str(model or "").strip(),
              str(l2 or "").strip() or str(cap or "").strip()):
        t = t.strip()
        if t and t not in caps:
            caps.append(t)
    ys = {1: [600], 2: [540, 705], 3: [460, 620, 780]}.get(len(caps), [600])
    # 型号居中且是最大一颗（2026-11-26 用户令「型号可以在中间最大的嘛」）：
    # 中间=型号 60 号，上 l1 48 号，下 l2 40 号
    bases = {3: [48, 60, 40], 2: [48, 60], 1: [56]}
    for t, cy, b0 in zip(caps, ys, bases.get(len(caps), [56] * len(caps))):
        pill_yellow(im, d, 540, cy, t, b0)
    ft = str(feat or "").strip()
    if ft:
        _fs = _fit(d, ft, 28, 940, bold=False)
        d.text((540, 1049), ft, font=R(_fs), fill=INK, anchor="mm")
    return im


def gen_features(cards, model, logo, points=None):
    """核心卖点 2×3 白卡六格：数据=蓝底 cfg.cells（(编号,标题,描述)），
    不足 6 张用聚光卖点行补（按「，/,/·」拆首段当标题）。"""
    im, d = canvas()
    cards = [tuple(c) for c in (cards or [])
             if isinstance(c, (list, tuple)) and len(c) >= 3][:6]
    if len(cards) < 6:
        for p0 in (points or []):
            if len(cards) >= 6:
                break
            s = str(p0).strip()
            if not s:
                continue
            seg = [x.strip() for x in s.replace("，", ",").split(",") if x.strip()]
            t1 = seg[0][:14] if seg else s[:14]
            t2 = " · ".join(seg[1:]) if len(seg) > 1 else ""
            if any(t1 == c[1] for c in cards):
                continue
            cards.append(("%02d" % (len(cards) + 1), t1, t2))
    header(im, d, "核心卖点", "KEY FEATURES",
           "%s · %d大亮点" % (model, max(1, len(cards))) if model else "", logo)
    for i, (no, title, desc) in enumerate(cards):
        # 数据源头判重（XQB120-BS50E 实锤）：set.json cells 的 dsc 与标题同文
        # （无逗号句 _v8_points 直给整句）→ 描述不画；标题被 nm[:12] 截断而描述
        # 是完整句（「(A计」+「(A计权)」）→ 用完整句当标题。
        t, ds = str(title).strip(), str(desc or "").strip()
        if ds and t and ds != t and ds.startswith(t):
            t, ds = ds, ""
        elif ds == t:
            ds = ""
        col, row = i % 2, i // 2
        x = 30 + col * 520
        y = 172 + row * 284
        d.rounded_rectangle((x, y, x + 500, y + 265), radius=16,
                            fill=CARD, outline=LIGHT, width=2)
        d.ellipse((x + 22, y + 72, x + 78, y + 128), fill=RED)
        d.text((x + 50, y + 100), str(no or "%02d" % (i + 1)),
               font=B(22), fill=(255, 255, 255), anchor="mm")
        _ts = _fit(d, t, 28, 380)
        d.text((x + 94, y + 64), t, font=B(_ts), fill=INK)
        if ds:
            _ds = _fit(d, ds, 19, 390, bold=False)
            d.text((x + 94, y + 112), ds, font=R(_ds), fill=GRAY)
        d.line((x + 20, y + 245, x + 480, y + 245), fill=LIGHT, width=1)
    return im


def gen_specs(rows, prod, model, logo):
    """机器参数：左列 8 行精选参数 + 右卡机器大图。"""
    im, d = canvas()
    header(im, d, "机器参数", "SPECIFICATIONS", model or "", logo)
    y = 190
    for label, val in (rows or [])[:8]:
        d.text((40, y), str(label), font=R(18), fill=GRAY)
        _vs = _fit(d, str(val), 28, 540)
        d.text((40, y + 28), str(val), font=B(_vs), fill=INK)
        d.line((40, y + 72, 580, y + 72), fill=LIGHT, width=2)
        y += 105
    d.rounded_rectangle((610, 175, 1050, 1015), radius=18,
                        fill=CARD, outline=LIGHT, width=2)
    if prod is not None:
        try:
            _paste_center(im, product_tight(prod, 400, 790), 830, 595)
        except Exception:
            pass
    return im


def gen_size(prod, sizeimg, dims, model, cap, brand, logo, sketch=None):
    """产品尺寸：左卡实物展示 + 右卡官方线稿原样贴（白卡黑线最保真，不再描摹重印）
    + 三格宽/高/深（铁律：宽高深必须印卡，缺数写「以实物为准」绝不编）+ 红条引导。"""
    im, d = canvas()
    header(im, d, "产品尺寸", "PRODUCT DIMENSIONS",
           ("%s · %s" % (model, cap) if cap else model) if model else "", logo)
    d.rounded_rectangle((30, 168, 470, 690), radius=18,
                        fill=CARD, outline=LIGHT, width=2)
    d.rounded_rectangle((490, 168, 1050, 690), radius=18,
                        fill=CARD, outline=LIGHT, width=2)
    d.text((50, 182), "实物展示", font=B(20), fill=RED)
    d.text((510, 182), "尺寸示意（正 / 侧视图）", font=B(20), fill=RED)
    if prod is not None:
        try:
            _paste_center(im, product_tight(prod, 400, 450), 250, 450)
        except Exception:
            pass
    _placed = False
    if sizeimg and os.path.isfile(str(sizeimg)):
        try:
            line = Image.open(str(sizeimg)).convert("RGB")
            sc = min(536 / float(max(1, line.width)), 460 / float(max(1, line.height)))
            tw = max(1, int(line.width * sc))
            th = max(1, int(line.height * sc))
            line = line.resize((tw, th), Image.LANCZOS)
            im.paste(line, (int(770 - tw / 2.0), int(445 - th / 2.0)))
            _placed = True
        except Exception:
            _placed = False
    if not _placed and sketch is not None:
        try:
            sc = min(500 / float(max(1, sketch.width)), 440 / float(max(1, sketch.height)))
            tw = max(1, int(sketch.width * sc))
            th = max(1, int(sketch.height * sc))
            sk = sketch.resize((tw, th), Image.LANCZOS)
            im.paste(sk, (int(770 - tw / 2.0), int(445 - th / 2.0)), sk)
            _placed = True
        except Exception:
            _placed = False
    if not _placed:
        d.text((770, 440), "尺寸以实物为准", font=R(22), fill=GRAY, anchor="mm")
    ok = False
    try:
        ok = bool(dims) and len(dims) >= 3 and all(
            str(x).strip() not in ("", "0", "None") for x in dims[:3])
    except Exception:
        ok = False
    triples = [("WIDTH", "宽 mm"), ("HEIGHT", "高 mm"), ("DEPTH", "深 mm")]
    if ok:
        nums = [str(int(x)) for x in dims[:3]]
    else:
        nums = ["以实物为准"] * 3
    for (en, cn), num, x in zip(triples, nums, [30, 375, 720]):
        d.rounded_rectangle((x, 708, x + 330, 878), radius=16,
                            fill=CARD, outline=LIGHT, width=2)
        cx = x + 165
        d.text((cx, 726), en, font=R(18), fill=GRAY, anchor="ma")
        if ok:
            d.text((cx, 752), num, font=B(56), fill=RED, anchor="ma")
        else:
            d.text((cx, 800), num, font=B(24), fill=GRAY, anchor="mm")
        d.text((cx, 842), cn, font=R(20), fill=INK, anchor="ma")
    footer(d, brand)
    return im
