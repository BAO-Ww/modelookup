# -*- coding: utf-8 -*-
"""首图（Hero 主图）本地绘制 + 参数补充
- 首图：1080x1080，跟随产品主色配色，产品图去白底居中+倒影，
        标题/副标题/胶囊标签全部取自抓取参数（不改写、不编造）
- 参数三层：官网参数 -> 兴达官方参数图视觉识别 -> 苏宁规格参数表
"""
import os, re, json, ssl, time, base64, colorsys, urllib.request, urllib.parse
from PIL import Image, ImageDraw, ImageFilter, ImageChops, ImageStat, ImageEnhance
import pei_core as core

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 品牌英文名（首图标题用：Leader 统帅电热水器）
BRAND_EN = {"海尔": "Haier", "卡萨帝": "Casarte", "统帅": "Leader",
            "haier": "Haier", "casarte": "Casarte", "leader": "Leader"}

# 包邮文案：全国版 / 报备版（深圳·东莞·惠州·广州）
SHIP_TEXT = {"N": "全国包邮", "B": "深圳·东莞·惠州·广州"}
SHIP_SHORT = {"N": "全国包邮", "B": "深圳/东莞/惠州/广州"}


# ---------------- 通用颜色工具 ----------------
def _mix(c1, c2, t):
    """把 c1 往 c2 混 t(0~1)"""
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1[:3], c2[:3]))


def _lum(c):
    return (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255.0


def product_tone(rgba):
    """取产品主色（过滤近白/近黑/低饱和像素后求均值）。
    取不到时返回浅灰蓝兜底。"""
    im = rgba.convert("RGBA")
    im.thumbnail((140, 140))
    px = im.load()
    w, h = im.size
    cols = []
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 128:
                continue
            L = _lum((r, g, b))
            if L > 0.92 or L < 0.08:
                continue                      # 白底/黑屏忽略
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / mx if mx else 0
            if sat < 0.08:
                continue                      # 中性灰忽略
            cols.append((sat, (r, g, b)))
    if len(cols) < 30:
        return (196, 206, 220)                # 白/黑产品兜底：浅灰蓝
    # 取饱和度最高的那批（金属产品里中性高光太多，会拉灰主色）
    cols.sort(key=lambda t: t[0], reverse=True)
    top = cols[:max(30, int(len(cols) * 0.35))]
    tone = tuple(sum(c[i] for _, c in top) // len(top) for i in range(3))
    # 再推一点饱和度，让背景有质感
    avg = sum(tone) / 3.0
    return tuple(max(0, min(255, int(avg + (c - avg) * 1.35))) for c in tone)


# ---------------- AI 背景（CogView 生成，缓存复用）----------------
def color_word(tone):
    """主色 -> 背景色系词（用于 AI 生成背景的 prompt）"""
    r, g, b = tone
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    if s < 0.10:
        return "浅灰"
    deg = h * 360
    if deg < 18 or deg >= 345:
        return "浅粉"
    if deg < 42:
        return "浅香槟"
    if deg < 72:
        return "浅米"
    if deg < 165:
        return "浅绿"
    if deg < 200:
        return "青蓝"
    if deg < 262:
        return "浅蓝"
    if deg < 305:
        return "浅紫"
    return "浅粉"


def color_word_dark(tone):
    """深色版色系词：color_word 给的是「浅灰/青蓝」，深色版要「深灰/深青蓝」。
    接近中性或过暗的颜色一律算深灰 —— 否则黑银家电那种 (41,43,47) 会被
    判成蓝色系，AI 就会把厨房背景画成蓝的。"""
    r, g, b = tone
    delta = max(r, g, b) - min(r, g, b)      # 用绝对色差判：饱和度在提亮后会被稀释
    if delta < 16:
        return "深灰"                         # 黑银家电 (41,43,47) 就是中性灰，别画成蓝的
    w = color_word(tone)
    if w.startswith("浅"):
        w = w[1:]
    return "深" + w


BG_PROMPT = ("极浅极淡的{c}色调，整体接近白色的柔和渐变背景，"
             "顶部有一束柔光，下方是浅色台面，大量留白，高级简约质感；"
             "不要深色、不要鲜艳色彩、不要强对比；"
             "不要边框、不要相框、不要几何形状、不要纹理图案；"
             "画面里不要出现任何物体、文字、logo")


def ai_background(tone, api_key, cache_dir, log=None):
    """用 CogView 生成背景（按色系缓存，同色系复用不重复生成）。
    返回 RGBA 图 或 None（失败时调用方回退本地渐变）。"""
    word = color_word(tone)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        return None
    fp = os.path.join(cache_dir, "bg_%s.jpg" % word)
    if os.path.exists(fp):
        try:
            return Image.open(fp).convert("RGBA")
        except Exception:
            pass
    if not api_key:
        return None
    try:
        body = {"model": "cogview-3-flash",
                "prompt": BG_PROMPT.format(c=word), "size": "1024x1024"}
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/images/generations",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + api_key.strip()})
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            d = json.loads(r.read())
        url = (d.get("data") or [{}])[0].get("url")
        if not url:
            return None
        raw = urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": UA}), timeout=90, context=ctx).read()
        bio = __import__("io").BytesIO(raw)
        im = Image.open(bio).convert("RGB")
        # 裁掉底部（免费模型右下角有「AI生成」水印）
        im = im.crop((0, 0, im.width, max(1, im.height - int(im.height * 0.115))))
        im = im.resize((W, H), Image.LANCZOS)
        # 重度模糊：AI 偶尔画出边框/物体，模糊后化成柔和光晕，只留氛围不抢产品
        im = im.filter(ImageFilter.GaussianBlur(42))
        # 统一提亮 + 降饱和：AI 偶尔给出偏暗背景，会吃掉文字对比度
        mean = ImageStat.Stat(im.convert("L")).mean[0]
        if mean < 232:
            im = ImageEnhance.Brightness(im).enhance(min(1.5, 238.0 / max(1.0, mean)))
        im = ImageEnhance.Color(im).enhance(0.82)
        im.save(fp, quality=92)
        if log:
            log("AI 背景已生成（%s 色系，亮度 %.0f）" % (word, mean))
        return im.convert("RGBA")
    except Exception as e:
        if log:
            log("AI 背景生成失败，改用渐变背景：%s" % repr(e)[:60])
        return None


# ---------------- 产品图去白底 ----------------
def cutout_white(img, thresh=28):
    """白底产品图去底：从四角 floodfill 标记背景（产品内部白色保留）。
    失败则回退阈值法。返回 RGBA。"""
    im = img.convert("RGBA")
    w, h = im.size
    out = im
    mark = im.convert("RGB").copy()
    try:
        for xy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
            ImageDraw.floodfill(mark, xy, (255, 0, 255), thresh=thresh)
        r, g, b = mark.split()
        mr = r.point(lambda v: 255 if v > 235 else 0)
        mg = g.point(lambda v: 255 if v < 45 else 0)
        mb = b.point(lambda v: 255 if v > 235 else 0)
        bgmask = ImageChops.multiply(ImageChops.multiply(mr, mg), mb)  # 洋红背景=255
        keep = ImageChops.invert(bgmask)                               # 产品=255
        out = im.copy()
        out.putalpha(ImageChops.multiply(im.getchannel("A"), keep))
    except Exception:
        out = im
    # 结果几乎为空 -> 回退阈值法
    if out.getchannel("A").getbbox() is None:
        mask = im.convert("L").point(lambda v: 0 if v > 244 else 255)
        out = im.copy()
        out.putalpha(mask)
    # 边缘羽化，避免硬边
    a = out.getchannel("A").filter(ImageFilter.GaussianBlur(0.8))
    out.putalpha(a)
    return out


def cutout_auto(img, log=None):
    """产品图去底。
    ① 原图自带透明通道（官网下载图几乎都是已抠好的 PNG，透明像素 >3%）-> 直接用它，最准。
       千万别先 convert("RGB")：那会把透明区压成白色/黑色，再当背景去抠，
       产品四周就会留一圈色块（机器两侧挂竖条色斑就是这么来的）。
    ② 真正的实底图 -> 按底色亮度选 floodfill 容差：暗底给宽容差（背景常有淡彩反光），
       亮底用默认 28 免得吃掉浅色机身。
    """
    im = img if img.mode == "RGBA" else img.convert("RGBA")
    total = max(1, im.width * im.height)
    try:
        if im.getchannel("A").histogram()[0] > total * 0.03:
            if log:
                log("原图自带透明通道，直接使用")
            return im
    except Exception:
        pass
    rgb = im.convert("RGB")
    w, h = rgb.size
    try:
        cs = [rgb.getpixel(p) for p in ((2, 2), (w - 3, 2), (2, h - 3), (w - 3, h - 3))]
        lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in cs) / 4.0
    except Exception:
        lum = 255.0
    thresh = 76 if lum < 48 else 28
    if log:
        log("实底图：底色亮度 %.0f -> 容差 %d" % (lum, thresh))
    return cutout_white(img, thresh=thresh)


def _trim_alpha(img, pad=6):
    """按 alpha 包围盒裁掉多余空白"""
    bbox = img.getchannel("A").getbbox()
    if not bbox:
        return img
    l, t, r, b = bbox
    l = max(0, l - pad); t = max(0, t - pad)
    r = min(img.width, r + pad); b = min(img.height, b + pad)
    return img.crop((l, t, r, b))


# ---------------- 首图绘制 ----------------
W = H = 1080
MARGIN = 56

# 比 cat 更具体的品类词（标题里出现就优先用）
CAT_DETAIL = ["电热水器", "燃气热水器", "空气能热水器", "壁挂式洗衣机", "洗干一体机",
              "滚筒洗衣机", "波轮洗衣机", "十字对开门冰箱", "对开门冰箱",
              "多门冰箱", "双门冰箱", "单门冰箱", "中央空调", "立式空调",
              "壁挂式空调", "抽油烟机", "燃气灶", "集成灶", "洗碗机",
              "电饭煲", "微波炉", "冷柜", "冰吧", "酒柜", "电视"]


def cat_label(info):
    """首图标题用的品类：优先标题里的具体词（电热水器 > 热水器）"""
    title = str(info.get("title") or "")
    for k in CAT_DETAIL:
        if k in title:
            return k
    return info.get("cat", "") or ""


def _draw_gradient(size, top, bottom):
    """垂直渐变背景"""
    w, h = size
    grad = Image.new("RGB", (1, h))
    gp = grad.load()
    for y in range(h):
        gp[0, y] = _mix(top, bottom, y / max(1, h - 1))
    return grad.resize((w, h), Image.BILINEAR)


def _pill(dr, box, radius, fill, outline=None, width=2):
    dr.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _fit(img, max_w, max_h):
    s = min(max_w / img.width, max_h / img.height)
    if s <= 0:
        return img
    return img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))),
                      Image.LANCZOS)


def _fit_font(dr, txt, f, max_w, min_size=13):
    """文字超宽就逐级缩小字号"""
    while dr.textlength(txt, font=f) > max_w and getattr(f, "size", 20) > min_size:
        f = core.font(f.size - 2, f.size and True)
    return f


def _spec_card(dr, box, k, v, tag_f, val_f, radius=14):
    """参数卡：淡阴影 + 白底圆角 + 标签(灰小) / 值(黑大)，窄卡自动缩字"""
    x0, y0, x1, y1 = box
    dr.rounded_rectangle([x0 + 2, y0 + 3, x1 + 2, y1 + 3], radius,
                         fill=(86, 76, 66, 26))          # 阴影
    dr.rounded_rectangle(box, radius, fill=(255, 255, 255, 216),
                         outline=(222, 216, 209, 255), width=2)
    cx = (x0 + x1) // 2
    inner = (x1 - x0) - 20
    fk = _fit_font(dr, k, tag_f, inner, 12)
    fv = _fit_font(dr, v, val_f, inner, 13)
    dr.text((cx, y0 + (y1 - y0) * 0.32), k, font=fk, fill="#8d867e", anchor="mm")
    dr.text((cx, y0 + (y1 - y0) * 0.70), v, font=fv, fill="#241f1b", anchor="mm")


def make_hero_cover(src_img, info, out_path, ship="N", specs_override=None,
                    api_key=None, use_ai_bg=True, bg_cache=None, log=None,
                    sell_price=None):
    """首图 1080x1080：AI 背景 + 真实产品图放大居中 + 左右参数卡。
    src_img : 产品图路径（白底/透明均可）
    info    : 抓取信息（brand/cat/model/price/props/title）
    ship    : 'N' 全国包邮 / 'B' 深圳东莞惠州广州
    """
    _src = Image.open(src_img)
    if max(_src.size) > 1100:            # 控制内存：去底+多次副本对大图很吃内存
        _src.thumbnail((1100, 1100), Image.LANCZOS)
    prod = _trim_alpha(cutout_white(core.load_rgb_img(_src)))
    tone = product_tone(prod)

    # ---------- 背景 ----------
    canvas = None
    if use_ai_bg and api_key:
        if bg_cache is None:
            bg_cache = os.path.join(os.path.dirname(os.path.abspath(out_path)), "_bg_cache")
        bg = ai_background(tone, api_key, bg_cache, log=log)
        if bg is not None:
            canvas = bg.copy()
    if canvas is None:
        top = _mix(tone, (255, 255, 255), 0.74)
        bot = _mix(tone, (255, 255, 255), 0.94)
        canvas = _draw_gradient((W, H), top, bot).convert("RGBA")
    dr = ImageDraw.Draw(canvas)

    # 版式自适应：竖高产品（洗衣机）卡片放机器两侧；横宽产品（热水器）卡片放底部
    ratio = prod.width / max(1, prod.height)
    is_tall = ratio < 1.35

    # ---------- 标题（英+中，带装饰）----------
    brand = info.get("brand", "") or ""
    ben = BRAND_EN.get(brand) or BRAND_EN.get(info.get("site", ""), "")
    cat = cat_label(info)
    specs = specs_override or info.get("props") or []
    tone_hex = "#%02x%02x%02x" % _mix(tone, (60, 50, 40), 0.45)   # 标题点缀色

    f_en = core.font(46, True)
    f_cn = core.font(56, True)
    en_w = dr.textlength(ben, font=f_en) if ben else 0
    cn_txt = (brand + cat)
    cn_w = dr.textlength(cn_txt, font=f_cn)
    gap = 18 if ben else 0
    total = en_w + gap + cn_w
    # 标题居中；竖高版式右上角有价格标 -> 限制标题宽度避免压字（保持居中）
    if is_tall:
        _pw = int(306 * 0.72)
        t_avail = 2 * (W // 2 - MARGIN - _pw - 10)
    else:
        t_avail = W - MARGIN * 2
    t_avail = max(360, min(t_avail, W - MARGIN * 2))
    if total > t_avail:
        scale = t_avail / total
        f_en = core.font(max(20, int(46 * scale)), True)
        f_cn = core.font(max(24, int(56 * scale)), True)
        en_w = dr.textlength(ben, font=f_en) if ben else 0
        cn_w = dr.textlength(cn_txt, font=f_cn)
        total = en_w + gap + cn_w
    sx = (W - total) // 2
    ty = 68 if is_tall else 74
    if ben:
        dr.text((sx, ty + 8), ben, font=f_en, fill=tone_hex, anchor="la")
        dr.text((sx + en_w + gap, ty), cn_txt, font=f_cn, fill="#1a1714", anchor="la")
    else:
        dr.text((sx, ty), cn_txt, font=f_cn, fill="#1a1714", anchor="la")

    # 副标题：核心规格 + 两侧装饰线
    tops = pick_specs(specs, cat, 3)
    subtitle = "   ·   ".join(v for _, v in tops[:3])
    if subtitle:
        f_sub = core.font(28, False)
        sw = dr.textlength(subtitle, font=f_sub)
        if sw > t_avail - 100:
            f_sub = core.font(max(15, int(28 * (t_avail - 100) / sw)), False)
            sw = dr.textlength(subtitle, font=f_sub)
        sy = 152
        t_cx = W // 2
        dr.text((t_cx, sy), subtitle, font=f_sub, fill="#5c5650", anchor="mm")
        lw = 40
        ln_y = sy + 1
        dr.line([t_cx - sw // 2 - 30 - lw, ln_y, t_cx - sw // 2 - 30, ln_y],
                fill=tone_hex, width=2)
        dr.line([t_cx + sw // 2 + 30, ln_y, t_cx + sw // 2 + 30 + lw, ln_y],
                fill=tone_hex, width=2)

    # ---------- 产品图（放大、正中）----------
    if is_tall:
        box_w, box_h = 760, 782
        area_top = 176
    else:
        box_w, box_h = 700, 560
        area_top = 184
    prod_fit = _fit(prod, box_w, box_h)
    px = (W - prod_fit.width) // 2
    py = area_top + (box_h - prod_fit.height) // 2
    sh = Image.new("RGBA", (prod_fit.width, 64), (0, 0, 0, 0))
    sdr = ImageDraw.Draw(sh)
    sdr.ellipse([prod_fit.width * 0.10, 0, prod_fit.width * 0.90, 56],
                fill=(60, 48, 38, 58))
    sh = sh.filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(sh, (px, py + prod_fit.height - 32))
    canvas.alpha_composite(prod_fit, (px, py))
    refl = prod_fit.transpose(Image.FLIP_TOP_BOTTOM)
    rw, rh = refl.size
    rh = max(1, int(rh * 0.24))
    refl = refl.crop((0, 0, rw, rh))
    fade = Image.new("L", (1, rh))
    fp = fade.load()
    for y in range(rh):
        fp[0, y] = max(0, int(32 * (1 - y / max(1, rh - 1))))
    fade = fade.resize((rw, rh))
    refl.putalpha(ImageChops.multiply(refl.getchannel("A"), fade))
    refl = refl.filter(ImageFilter.GaussianBlur(5))
    canvas.alpha_composite(refl, (px, py + prod_fit.height - 4))

    # ---------- 左右参数卡（各 3~4 个，浮在产品两侧）----------
    cells = [(str(k), str(v)) for k, v in specs
             if v and len(str(v)) <= 12 and str(k)]
    # 按品类优先级排序，最多 8 项（左 4 右 4；不足则各 3）
    ordered = pick_specs(specs, cat, 8)
    seen = set()
    cells = []
    for k, v in ordered:
        if (k, v) not in seen:
            seen.add((k, v))
            cells.append((k, v))
    for k, v in specs:                      # 补足到 8
        if len(cells) >= 8:
            break
        kk, vv = str(k), str(v)
        if vv and len(vv) <= 12 and (kk, vv) not in seen:
            seen.add((kk, vv))
            cells.append((kk, vv))
    cells = cells[:8]
    tag_f = core.font(17, False)
    val_f = core.font(25, True)
    n_left = (len(cells) + 1) // 2
    if is_tall:
        # 竖高产品：机器左右各一列（产品两侧有空间）
        cw, ch, gap_y = 172, 78, 15
        band_top = 800
        for side, group in ((0, cells[:n_left]), (1, cells[n_left:])):
            gx = 20 if side == 0 else W - 20 - cw
            total_h = len(group) * ch + max(0, len(group) - 1) * gap_y
            gy = 176 + (782 - total_h) // 2
            for k, v in group:
                _spec_card(dr, [gx, gy, gx + cw, gy + ch], k, v, tag_f, val_f)
                gy += ch + gap_y
    else:
        # 横宽产品：卡片放底部（价格条两侧），不压机身
        cw, ch, gap_x, gap_y = 150, 80, 12, 16
        band_top = 792
        for side, group in ((0, cells[:n_left]), (1, cells[n_left:])):
            gx0 = 20 if side == 0 else W - 20 - (2 * cw + gap_x)
            for i, (k, v) in enumerate(group):
                r, c = divmod(i, 2)
                bx = gx0 + c * (cw + gap_x)
                by = band_top + r * (ch + gap_y)
                _spec_card(dr, [bx, by, bx + cw, by + ch], k, v, tag_f, val_f)

    # ---------- 价格条（底部居中）----------
    official = info.get("price") or 0
    price = sell_price or official or 0
    has_cmp = bool(sell_price and official and int(sell_price) != int(official))
    k = 0.72 if is_tall else 1.0
    card_w = int(306 * k)
    card_h = int((122 if has_cmp else 78) * k)
    if is_tall:
        cx = W - 20 - card_w          # 右上角
        cy = 24
    else:
        cx = (W - card_w) // 2
        cy = band_top + (2 * ch + gap_y - card_h) // 2
    dr.rounded_rectangle([cx, cy, cx + card_w, cy + card_h], 22, fill="#2b211a")
    ship_txt = "全新未拆封 · " + SHIP_TEXT.get(ship, SHIP_TEXT["N"])
    if price:
        f_yen = core.font(int(20 * k), True)
        f_num = core.font(int(36 * k), True)
        ptxt = format(int(price), ",")
        wtxt = dr.textlength(ptxt, font=f_num)
        yw = dr.textlength("¥", font=f_yen)
        total = yw + 5 + wtxt
        sx2 = cx + (card_w - total) // 2
        py2 = cy + int((31 if has_cmp else 28) * k)
        dr.text((sx2, py2), "¥", font=f_yen, fill="#e8c17a", anchor="lm")
        dr.text((sx2 + yw + 5, py2 - 3), ptxt, font=f_num, fill="#ffffff", anchor="lm")
        # 官网价（划线）：让买家一眼看出便宜多少
        if has_cmp:
            cmp_txt = "官网价 ¥%s" % format(int(official), ",")
            f_cmp = core.font(max(12, int(17 * k)), False)
            cw2 = dr.textlength(cmp_txt, font=f_cmp)
            x_cmp = cx + (card_w - cw2) // 2
            y_cmp = cy + int(62 * k)
            dr.text((x_cmp, y_cmp), cmp_txt, font=f_cmp, fill="#9d958d", anchor="la")
            dr.line([x_cmp, y_cmp + 11, x_cmp + cw2, y_cmp + 11], fill="#9d958d", width=1)
            ship_y = cy + card_h - int(18 * k)
        else:
            ship_y = cy + 74
        f_ship = core.font(max(12, int(17 * k)), True)
        sw2 = dr.textlength(ship_txt, font=f_ship)
        if sw2 > card_w - 24:
            f_ship = core.font(max(11, int(17 * k * (card_w - 20) / sw2)), True)
        dr.text((cx + card_w // 2, ship_y), ship_txt, font=f_ship,
                fill="#f0d9a8", anchor="mm")
    else:
        dr.text((cx + card_w // 2, cy + card_h // 2), ship_txt,
                font=core.font(17, True), fill="#f0d9a8", anchor="mm")

    # ---------- 水印 + 底部小字 ----------
    note_y = 1016 if is_tall else band_top + 2 * ch + gap_y + 46
    dr.text((W // 2, note_y), "【签收】当面验货，先验货后签收，破损请拒收",
            font=core.font(25, False), fill="#6f6862", anchor="mm")

    out = core.load_rgb_img(canvas)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out.save(out_path, quality=95)
    return out_path


def pick_product_img(info, sel_dir=None):
    """挑首图用的产品原图：精选目录第一张 -> 素材目录第一张（排除 0_ 产物）"""
    dirs = []
    if sel_dir:
        dirs.append(sel_dir)
    if info.get("dir"):
        dirs.append(info["dir"])
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        fs = [f for f in sorted(os.listdir(d))
              if f.lower().endswith((".jpg", ".jpeg", ".png"))
              and not f.startswith("0_") and not f.startswith("_")]
        if fs:
            return os.path.join(d, fs[0])
    return None


# ---------------- 参数挑选（按品类优先级）----------------
SPEC_ORDER = {
    "热水器": ["容积", "容量", "加热方式", "款式", "能效", "最高温度", "加热功率"],
    "洗衣机": ["洗涤容量", "洗衣容量", "容量", "烘干容量", "最高转速", "能效",
             "电机", "变频", "洗烘"],
    "冰箱": ["总容积", "容积", "制冷方式", "能效", "尺寸", "变频", "面板"],
    "冷柜": ["容积", "制冷方式", "能效", "尺寸"],
    "空调": ["匹", "能效", "变频", "自清洁", "冷暖"],
    "电视": ["尺寸", "英寸", "分辨率", "能效"],
    "厨电": ["容量", "功率", "能效", "尺寸"],
    "冰吧": ["容积", "制冷方式", "能效"],
    "酒柜": ["容积", "制冷方式", "能效"],
    "电器": ["容积", "容量", "能效", "尺寸", "功率"],
}


def _order_for(cat):
    """品类 -> 参数优先级表（支持 电热水器/燃气热水器 等具体词）"""
    c = str(cat or "")
    for key, order in SPEC_ORDER.items():
        if key in c:                      # 电热水器 -> 热水器
            return order
    for kw, key in (("热水器", "热水器"), ("洗衣", "洗衣机"), ("冰箱", "冰箱"),
                    ("冷柜", "冷柜"), ("空调", "空调"), ("电视", "电视"),
                    ("油烟", "厨电"), ("灶", "厨电"), ("洗碗", "厨电"),
                    ("冰吧", "冰吧"), ("酒柜", "酒柜")):
        if kw in c:
            return SPEC_ORDER[key]
    return SPEC_ORDER["电器"]


def pick_specs(props, cat, n=3, used_vals=None):
    """按品类优先级从参数里挑 n 条（返回 [(k,v)]，v 原文不改）"""
    order = _order_for(cat)
    used_vals = set(used_vals or ())
    out = []
    def short(v):
        return len(str(v)) <= 12          # 尺寸这类长值不上海报

    for key in order:
        for k, v in props:
            if not v or v in used_vals or not short(v):
                continue
            if key in k:
                out.append((k, v))
                used_vals.add(v)
                break
        if len(out) >= n:
            break
    # 不足时用剩余参数补齐
    if len(out) < n:
        for k, v in props:
            if v and v not in used_vals and short(v):
                out.append((k, v))
                used_vals.add(v)
            if len(out) >= n:
                break
    return out[:n]


# ---------------- 参数三层补充 ----------------
# 参数名白名单（视觉识别纠错用：把识别错的词形拉回来）
SPEC_FIX = [
    ("制冷容量", "洗涤容量"), ("洗涤容量", "洗涤容量"),
    ("洗衣容量", "洗涤容量"), ("洗涤剂容量", "洗涤容量"),
    ("脱水容量", "脱水容量"), ("烘干容量", "烘干容量"),
    ("制冷剂", "制冷剂"), ("额定制冷量", "额定制冷量"),
]


def correct_specs(pairs):
    """参数名纠错：视觉模型偶尔把「洗涤」认成「制冷」等"""
    out = []
    for k, v in pairs:
        k = str(k).strip().replace(" ", "").replace("：", "").replace(":", "")
        v = str(v).strip()
        for wrong, right in SPEC_FIX:
            if k == wrong:
                k = right
                break
        if k and v:
            out.append((k, v))
    return out


def vision_specs(img_paths, api_key, log=None, max_try=4):
    """用智谱 GLM-4V 从官方长图里找参数表并提取。
    逐张试（参数图通常在官方图靠后），命中即停。"""
    if not api_key:
        return []
    from PIL import Image as _I
    # 参数图通常靠后：先试最后 max_try 张
    cands = list(img_paths)[-max_try:]
    for p in reversed(cands):
        try:
            im = _I.open(p)
            if im.width < 300 or im.height < 300:
                continue
            im.thumbnail((1400, 1400))
            buf = __import__("io").BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=88)
            b64 = base64.b64encode(buf.getvalue()).decode()
            body = {
                "model": "glm-4v-flash",
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": b64}},
                    {"type": "text", "text":
                     "这是一张家电详情图。如果图中包含「产品参数」表格，"
                     "请严格按「参数名: 值」每行一条输出全部参数；"
                     "如果不是参数表，只回复 NO。"}]}],
            }
            req = urllib.request.Request(
                "https://open.bigmodel.cn/api/paas/v4/chat/completions",
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + api_key.strip()})
            with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
                d = json.loads(r.read())
            txt = (d.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if not txt or txt.strip().upper().startswith("NO"):
                continue
            pairs = []
            for line in txt.split("\n"):
                line = line.strip().strip("-•· ").replace("：", ":")
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k, v = k.strip(), v.strip()
                if 1 < len(k) < 14 and 0 < len(v) < 40:
                    pairs.append((k, v))
            if len(pairs) >= 4:
                pairs = correct_specs(pairs)
                if log:
                    log("参数图识别成功：%s（%d 项）" % (os.path.basename(p), len(pairs)))
                return pairs
            if log:
                log("  %s 不是参数图" % os.path.basename(p))
        except Exception as e:
            if log:
                log("  参数图识别失败 %s: %s" % (os.path.basename(p), repr(e)[:50]))
            continue
    return []


def suning_specs(model, log=None):
    """苏宁规格参数表（精确匹配型号才有）"""
    try:
        kw = str(model).strip()
        if not kw:
            return []
        u = "https://search.suning.com/" + urllib.parse.quote(kw) + "/"
        req = urllib.request.Request(u, headers={"User-Agent": UA,
                                                "Accept-Language": "zh-CN"})
        d = urllib.request.urlopen(req, timeout=20, context=ctx).read().decode("utf-8", "ignore")
        key = kw.replace(" ", "").upper()
        pids = re.findall(r'href="(//product\.suning\.com/[\d]+/[\d]+\.html)"', d)
        for pid in dict.fromkeys(pids):
            pu = "https:" + pid.split("#")[0]
            pd = urllib.request.urlopen(urllib.request.Request(
                pu, headers={"User-Agent": UA, "Referer": "https://search.suning.com/"}),
                timeout=20, context=ctx).read().decode("utf-8", "ignore")
            t = re.search(r"<title>(.*?)</title>", pd, re.S)
            ti = t.group(1) if t else ""
            if key not in ti.replace(" ", "").upper():
                continue
            i = pd.find("规格参数")
            if i < 0:
                continue
            seg = pd[i:i + 6000]
            items = re.findall(r">([^<>]{2,20})<", seg)
            pairs = []
            for a, b in zip(items, items[1:]):
                if 1 < len(a) < 14 and 0 < len(b) < 40 and not b.endswith("："):
                    if a not in ("规格参数",) and not a.startswith(("展开", "收起")):
                        pairs.append((a, b))
            if len(pairs) >= 4:
                if log:
                    log("苏宁参数命中：%s（%d 项）" % (ti.strip()[:24], len(pairs)))
                return correct_specs(pairs)
    except Exception as e:
        if log:
            log("苏宁参数获取失败：%s" % repr(e)[:50])
    return []


def enrich_specs(info, api_key=None, log=None, img_paths=None):
    """三层补充参数（在官网参数不足时调用），返回补后的 props"""
    props = list(info.get("props") or [])
    if len(props) >= 6:
        return props
    if log:
        log("参数不足（%d 项），尝试补充 ..." % len(props))
    # 第二层：兴达官方参数图
    if img_paths:
        got = vision_specs(img_paths, api_key, log=log)
        if got:
            return merge_specs(props, got)
    # 第三层：苏宁
    got2 = suning_specs(info.get("model", ""), log=log)
    if got2:
        return merge_specs(props, got2)
    return props


def merge_specs(base, extra):
    """合并参数：base 优先，extra 补空缺（同名不覆盖，值不同则保留 base）"""
    out = list(base)
    keys = {k for k, _ in out}
    for k, v in extra:
        if k not in keys:
            out.append((k, v))
            keys.add(k)
    return out


# 促销风首图（黑金版式）
# 设计依据（闲鱼买家 0.3-0.5 秒决策）：
#   0.1s「这是什么」-> 产品大图居中，一眼认出来
#   0.2s「多少钱」  -> 底部黑价格块 + 右上爆炸贴（省了多少比原价多少更勾人）
#   0.3s「靠谱吗」  -> 顶部黑条「全新未拆封 / 全国联保」，像正经家电店
# 硬规则：产品必须用原图抠图，不让 AI 重画；参数与价格全部真实，不编造。
# ============================================================================
PROMO_BG = "#f2f2f2"
PROMO_INK = "#121212"
PROMO_GRAY = "#8c8c8c"
PROMO_LINE = "#dedede"
PROMO_GOLD = "#f0c24b"
PROMO_LIME = "#d8f24e"

_PROMO_LEFT_PRI = ("洗涤容量", "脱水容量", "烘干容量", "容积", "容量", "转速", "筒径",
                   "尺寸", "洗净比", "匹数", "功率", "风量", "加湿量")
_PROMO_RIGHT_PRI = ("电机", "变频", "直驱", "能效", "除菌", "抗菌", "防霉", "消毒",
                    "静音", "噪音", "自洁", "洗涤", "烘干", "火力", "热负荷",
                    "熄火", "静压", "风量", "智能", "快洗", "添衣", "去污", "预约")
_PROMO_SKIP = ("净重", "毛重", "重量", "包装", "上市", "型号", "产品类型",
               "机身颜色", "面板颜色", "产品系列", "额定", "上部外形")
# 值为这些词时本身没有卖点信息，标题改用参数名
_PROMO_MEANINGLESS = ("支持", "有", "是", "无", "否", "不支持", "-", "—", "")
# 能效等级换成更有卖点的说法
_PROMO_LEVEL = {"1级": "一级能效", "一级": "一级能效", "2级": "二级能效",
                "二级": "二级能效", "3级": "三级能效", "三级": "三级能效"}

_PROMO_NOTE = (("直驱", "稳定静音"), ("变频", "运行平稳"), ("能效", "节能省电"),
               ("除菌", "健康护衣"), ("抗菌", "洁净卫生"), ("防霉", "不易发霉"),
               ("静音", "静音不扰眠"), ("自洁", "免手动清洗"), ("烘干", "即洗即穿"),
               ("洗涤", "洁净如新"), ("智能", "省心省力"), ("快洗", "快速出门"),
               ("添衣", "随时加衣"), ("去污", "强力去渍"), ("预约", "错峰省电"))


def _promo_note(text):
    for k, note in _PROMO_NOTE:
        if k in str(text):
            return note
    return ""


def _short_name(k):
    """参数名做左列小标签：去掉括号里的补充说明，太长就截断"""
    t = re.sub(r"[（(][^）)]*[）)]", "", str(k))
    t = re.sub(r"[（(].*$", "", t)
    return t.strip()[:9]


def _promo_pick(props, pri, n=4, dedup_val=False, num_only=False):
    """按优先级从真实参数里挑 n 项。
    dedup_val : 相同值的只留一条（左列避免出现两个 10kg）
    num_only  : 只挑含数字的规格（左列专属，防止把「直驱变频」混进来）
    """
    pool = [(str(k), str(v)) for k, v in props
            if v and len(str(v)) <= 20
            and not any(s in str(k) for s in _PROMO_SKIP)]
    out, used, seen = [], set(), set()

    def _take(k, v):
        if dedup_val and v in seen:
            return False
        if num_only and not re.search(r"\d", v):
            return False
        out.append((k, v))
        used.add(k)
        seen.add(v)
        return True

    for key in pri:
        for k, v in pool:
            if k in used or not (key in k or key in v):
                continue
            if _take(k, v):
                break
        if len(out) >= n:
            break
    for k, v in pool:
        if len(out) >= n:
            break
        if k not in used:
            _take(k, v)
    return out[:n]


# 各品类的真实场景（AI 背景用）。只生成环境，不生成产品——产品必须原图抠图
PROMO_SCENE = {
    "洗衣机": "现代洗衣房的一角，有台面和收纳柜，柔和自然光",
    "冰箱": "开放式厨房的一角，有台面和橱柜，柔和自然光",
    "冰柜": "现代厨房的一角，有柜体，柔和自然光",
    "空调": "简约卧室的一角，有墙面和窗帘，柔和日光",
    "热水器": "现代卫浴间的一角，有瓷砖墙面，柔和光线",
    "油烟机": "现代厨房的一角，有橱柜，柔和自然光",
    "燃气灶": "现代厨房的一角，有石材台面，柔和自然光",
    "集成灶": "现代厨房的一角，有石材台面和橱柜，柔和环境光",
    "厨电": "现代厨房的一角，有石材台面和橱柜，柔和环境光",
    "洗碗机": "现代厨房的一角，有橱柜，柔和自然光",
    "电视": "简约客厅的一角，有墙面和地板，柔和自然光",
    "通用": "极简室内空间的一角，有柔和自然光，干净简洁",
}


def promo_scene_bg(cat, api_key, cache_dir, log=None, dark=False, tone=None):
    """AI 场景背景（CogView 文生图）。
    只画环境不画产品；dark=True 出深色调场景（配合深色版），否则出明亮浅色场景。
    任何一步失败都返回 None，调用方回退纯色/渐变底，不会卡住。
    """
    if not api_key:
        return None
    key = str(cat or "")
    prompt = PROMO_SCENE["通用"]
    for kw, p in PROMO_SCENE.items():          # 包含匹配：滚筒洗衣机 -> 洗衣机
        if kw == "通用" or not kw:
            continue
        if kw in key:
            prompt = p
            break
    if dark:
        if tone:
            prompt += ("；整体呈%s色调，暗调氛围，只有柔和环境光，不要明亮高光；"
                       % color_word_dark(tone))
        else:
            prompt += ("；整体深色调，深灰黑主色，暗调氛围，只有柔和环境光，不要明亮高光；")
        prompt += "不要出现任何文字、logo、人物、电器产品、边框"
    else:
        prompt += ("；画面干净简洁、高明度浅色；"
                   "不要出现任何文字、logo、人物、电器产品、边框")
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        return None
    import hashlib
    # ⚠️ 改了后处理逻辑就要把版本号 +1，否则会命中旧缓存图（缓存的是成品）
    fp = os.path.join(cache_dir, "scene_bgv3_%s_%s.jpg" % (
        "d" if dark else "l",
        hashlib.md5(prompt.encode("utf-8")).hexdigest()[:10]))
    if os.path.exists(fp):
        try:
            return Image.open(fp).convert("RGBA")
        except Exception:
            pass
    try:
        body = {"model": "cogview-3-flash", "prompt": prompt, "size": "1024x1024"}
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/images/generations",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + str(api_key).strip()})
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            d = json.loads(r.read())
        url = (d.get("data") or [{}])[0].get("url")
        if not url:
            return None
        raw = urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": UA}), timeout=90, context=ctx).read()
        im = Image.open(__import__("io").BytesIO(raw)).convert("RGB")
        im = im.crop((0, 0, im.width,
                      max(1, im.height - int(im.height * 0.115))))  # 裁水印
        im = im.resize((2048, 2048), Image.LANCZOS)
        if dark:
            im = im.filter(ImageFilter.GaussianBlur(26))
            mean = ImageStat.Stat(im.convert("L")).mean[0]
            if mean > 92:                       # 太亮 -> 压进深色区间，白字才立得住
                im = ImageEnhance.Brightness(im).enhance(
                    max(0.42, 88.0 / max(1.0, mean)))
            elif mean < 54:                     # 太暗 -> 提一点，别糊成纯黑一片
                im = ImageEnhance.Brightness(im).enhance(
                    min(1.6, 62.0 / max(1.0, mean)))
            im = ImageEnhance.Color(im).enhance(0.62)
            im = Image.blend(im, Image.new("RGB", im.size, (24, 24, 28)), 0.28)
        else:
            im = im.filter(ImageFilter.GaussianBlur(30))
            mean = ImageStat.Stat(im.convert("L")).mean[0]
            if mean < 236:
                im = ImageEnhance.Brightness(im).enhance(
                    min(1.6, 240.0 / max(1.0, mean)))
            im = ImageEnhance.Color(im).enhance(0.70)
            im = Image.blend(im, Image.new("RGB", im.size, (255, 255, 255)), 0.42)
        im.save(fp, quality=92)
        if log:
            log("AI 场景背景已生成（%s%s）" % (key, "·深色" if dark else ""))
        return im.convert("RGBA")
    except Exception as e:
        if log:
            log("AI 场景背景失败，改用渐变底：%s" % repr(e)[:60])
        return None


def promo_studio_bg(model, api_key, cache_dir, log=None, tone=None, seed=None, flavor=None,
                   bright=False):
    """AI 影棚背景（CogView 文生图），用于「发布图」套图。

    bright=False（默认）：克制的高级深灰影棚（无缝背景 + 椭圆光池），**不是厨房场景**，
        同色系靠 tone 微调，叠加 seed 保证每台机器背景各不相同；绝不出白底。
    bright=True：明亮白色极简影棚（浅灰→纯白柔和渐变），用于「亮白影棚」风格，
        文字改用深色，机器投影分离；仍无产品无文字无边框。
    任何一步失败都返回 None，调用方回退程序渐变底。
    同色系靠 tone 微调，并叠加 seed（每台机型的稳定微差）保证「每台机器背景
    由 AI 按机型生成、同冷灰族但各不相同」；绝不出白底。
    任何一步失败都返回 None，调用方回退程序渐变底。
    """
    if not api_key:
        return None
    key = str(model or "")
    prompt = ("高级感深色影棚产品摄影背景，深灰炭黑无缝背景，"
              "顶部柔光在地面形成一道柔和的椭圆光池，电影感柔和过渡，"
              "绝对无图案、无亮片、无条纹、无纹理、无边框、无文字、无产品、无人物")
    if tone:
        prompt += ("；整体呈%s色调，暗调氛围，只有柔和环境光，不要明亮高光"
                   % color_word_dark(tone))
    # 2026-09-15 用户要求：背景要「按机型生成、各不相同」。seed 由机型确定性派生，
    # 只微调光池位置/冷灰微差，仍严格保持深灰冷灰高级影棚族（不跑出色系）。
    if seed:
        prompt += ("；" + seed)
    # 「机器更显眼」模式（pop）：按品类注入一句自由发挥的风格短语（大理石/金属/岩板…），
    # 让背景更有高级感、各不相同；仍严格保持深灰冷灰、无白底无文字。
    if flavor:
        prompt += ("；" + flavor)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception:
        return None
    import hashlib
    fp = os.path.join(cache_dir, "studio_bgv1_%s.jpg"
                      % hashlib.md5(prompt.encode("utf-8")).hexdigest()[:10])
    if os.path.exists(fp):
        try:
            return Image.open(fp).convert("RGBA")
        except Exception:
            pass
    try:
        body = {"model": "cogview-3-flash", "prompt": prompt, "size": "1024x1024"}
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/images/generations",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + str(api_key).strip()})
        with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
            d = json.loads(r.read())
        url = (d.get("data") or [{}])[0].get("url")
        if not url:
            return None
        raw = urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": UA}), timeout=90, context=ctx).read()
        im = Image.open(__import__("io").BytesIO(raw)).convert("RGB")
        im = im.crop((0, 0, im.width,
                      max(1, im.height - int(im.height * 0.115))))  # 裁水印
        im = im.resize((2048, 2048), Image.LANCZOS)
        im = im.filter(ImageFilter.GaussianBlur(26))
        mean = ImageStat.Stat(im.convert("L")).mean[0]
        if bright:
            # 亮白影棚：整体向白色提亮，白字不立得住（调用方会改用深色字）
            if mean < 236:
                im = ImageEnhance.Brightness(im).enhance(
                    min(1.6, 240.0 / max(1.0, mean)))
            im = ImageEnhance.Color(im).enhance(0.55)
            im = Image.blend(im, Image.new("RGB", im.size, (255, 255, 255)), 0.45)
        else:
            if mean > 92:                       # 太亮 -> 压进深色区间，白字才立得住
                im = ImageEnhance.Brightness(im).enhance(
                    max(0.42, 88.0 / max(1.0, mean)))
            elif mean < 54:                     # 太暗 -> 提一点，别糊成纯黑一片
                im = ImageEnhance.Brightness(im).enhance(
                    min(1.6, 62.0 / max(1.0, mean)))
            im = ImageEnhance.Color(im).enhance(0.62)
            im = Image.blend(im, Image.new("RGB", im.size, (24, 24, 28)), 0.28)
        im.save(fp, quality=92)
        if log:
            log("AI 影棚背景已生成（%s%s）" % (key, "·亮白" if bright else ""))
        return im.convert("RGBA")
    except Exception as e:
        if log:
            log("AI 影棚背景失败，改用渐变底：%s" % repr(e)[:60])
        return None


# 深色版配色（整体深色，靠产品背后光晕分离主体）
DARK_TOP = (30, 30, 33)     # 顶部近黑
DARK_BOT = (88, 88, 95)     # 底部中深灰（不是浅灰！产品背后必须保持深色）
DARK_BAR = "#1e1e1e"
DARK_WHITE = "#ffffff"
DARK_SUB = "#c4c4ca"        # 参数列小字 / 型号
DARK_DIM = "#b4b4bc"        # 最底部配送说明
GOLD_HI = "#ffe664"         # 爆炸贴金额 (255,230,100)
PINK_LINE = "#ff6b9d"       # 划线价
YELLOW_GO = "#ffd93d"       # 捡漏！


def _promo_glow(W, H, cx, cy, gw, gh, strength=46):
    """产品背后的柔和聚光（径向），让机身从深色背景里浮出来。

    ⚠️ 椭圆到画布的边距必须大于模糊半径，否则模糊会被画布边缘切掉，
    光晕就变成「带直角硬边的矩形」——表现为机器外围挂着一个比它宽一圈的方块。
    这里边距 96px、模糊 42px，留足余量。
    """
    S, PAD, BLUR = 384, 96, 42
    g = Image.new("L", (S, S), 0)
    ImageDraw.Draw(g).ellipse([PAD, PAD, S - PAD, S - PAD], fill=strength)
    g = g.filter(ImageFilter.GaussianBlur(BLUR)).resize((gw, gh), Image.LANCZOS)
    lay = Image.new("RGBA", (gw, gh), (255, 255, 255, 0))
    lay.putalpha(g)
    return lay, (cx - gw // 2, cy - gh // 2)


def make_hero_promo(src_img, info, out_path, ship="N", sell_price=None, log=None,
                    api_key=None, use_ai_bg=False, bg_cache=None):
    """促销风首图 · 深色版（2048x2048）
    背景用产品主色压暗的同色系深色渐变（每台机器颜色不同）；use_ai_bg=True 时可改用 AI 场景。
    产品直接铺已抠好的机图，背后不加任何光晕（光晕模糊会被画布边缘切出硬边方块）。
    版面（2026-09-15 用户定稿）：**不画任何参数列/卖点胶囊/大数字块**，
    只留 顶栏 + 品牌(大)+型号小字 + 大机器 + 价格块 + 发货地；
    机器框 1560x1400，尽可能占满画面（去掉了 AI5张 模板遗留的左右参数列后才有这个空间）。
    """
    W = H = 2048

    # ---------- 产品图先加载：背景配色要跟着产品主色走 ----------
    _src = Image.open(src_img)
    if max(_src.size) > 2400:
        _src.thumbnail((2400, 2400), Image.LANCZOS)
    # 传原图进去（不要先转 RGB，否则自带透明通道会被压掉）
    prod = _trim_alpha(cutout_auto(_src, log=log))
    tone = product_tone(prod)          # 产品主色 -> 决定背景色系（每台机器不一样）
    # 提亮一档保住色相：黑银家电主色接近纯黑，直接拿它调背景会糊成死黑
    tint = _mix(tone, (255, 255, 255), 0.45)

    # ---------- 背景：AI 同色系深色场景 -> 回退产品主色深色渐变 ----------
    canvas = None
    if use_ai_bg and api_key:
        if bg_cache is None:
            bg_cache = os.path.join(os.path.dirname(os.path.abspath(out_path)),
                                    "_bg_cache")
        canvas = promo_scene_bg(cat_label(info), api_key, bg_cache,
                                log=log, dark=True, tone=tone)
    if canvas is None:
        # 产品主色压暗成同色系深色渐变，而不是千篇一律的灰
        g_top = _mix(tint, (24, 24, 28), 0.84)
        g_bot = _mix(tint, (92, 92, 98), 0.50)
        canvas = _draw_gradient((W, H), g_top, g_bot).convert("RGBA")
        if log:
            log("用产品主色渐变底（%s系，主色 RGB%s）"
                % (color_word_dark(tone), tone))
    # 兜底再压一次：偏亮会吃掉白字和产品轮廓
    mean = ImageStat.Stat(canvas.convert("L")).mean[0]
    if mean > 94:
        canvas = ImageEnhance.Brightness(canvas.convert("RGB")).enhance(
            max(0.42, 88.0 / max(1.0, mean))).convert("RGBA")
    dr = ImageDraw.Draw(canvas)

    brand = info.get("brand") or "海尔"
    model = (info.get("model") or "").strip()
    props = list(info.get("props") or [])
    series = ""
    for k, v in props:
        if "系列" in str(k):
            series = str(v)
            break

    official = info.get("price") or 0
    price = sell_price or official or 0
    has_cmp = bool(sell_price and official and int(sell_price) != int(official))
    save_amt = (int(official) - int(price)) if has_cmp else 0

    # ---------- 1. 顶栏黑条（信任前置）----------
    bar_h = 110
    dr.rectangle([0, 0, W, bar_h], fill=DARK_BAR)
    dr.text((64, bar_h // 2 + 2), "全新未拆封", font=core.font(62, True),
            fill=DARK_WHITE, anchor="lm")
    dr.text((W - 64, bar_h // 2 + 2), "全国联保", font=core.font(51, True),
            fill=DARK_WHITE, anchor="rm")

    # ---------- 2. 左上角品牌区（白字，深色底上）----------
    ben = BRAND_EN.get(brand, "")
    bx, by = 64, 200
    if ben:
        f_en = core.font(57, True)
        dr.text((bx, by), ben, font=f_en, fill=DARK_WHITE, anchor="lm")
        bx += int(dr.textlength(ben, font=f_en)) + 22
    dr.text((bx, by), brand, font=core.font(66, True), fill=DARK_WHITE, anchor="lm")
    dr.text((64, by + 64), model + (" · %s系列" % series if series else ""),
            font=core.font(36, False), fill=DARK_SUB, anchor="lm")

    # ---------- 3. 右上爆炸贴 ----------
    if has_cmp and save_amt > 0:
        ecx, ecy, er = W - 182, 240, 105
        dr.ellipse([ecx - er, ecy - er, ecx + er, ecy + er], fill=DARK_BAR)
        dr.text((ecx, ecy - 33), "直降", font=core.font(28, True),
                fill=DARK_WHITE, anchor="mm")
        dr.text((ecx, ecy + 30), "¥%d" % save_amt, font=core.font(46, True),
                fill=GOLD_HI, anchor="mm")

    # ---------- 4. 产品（唯一主角，占满可画区域）----------
    # 2026-09-15 用户要求：① 去掉 AI5张 模板遗留的左右「参数列」② 机器放大。
    # 去掉参数列后左右两侧腾空，产品框从 860x1320 放到 1560x1400。
    # 竖向安全区：顶栏 0-110、品牌区 ~170-282；底部价格块 1732-1929、发货 1990。
    # 故产品可用区 = 300 ~ 1712（box_h 1400 时底边 1700，仍留 32px 给价格块）。
    box_w, box_h, area_top = 1560, 1400, 300
    pf = _fit(prod, box_w, box_h)
    px = (W - pf.width) // 2
    py = area_top + (box_h - pf.height) // 2
    base_y = py + pf.height

    dr.rounded_rectangle([px - 52, base_y - 10, px + pf.width + 52, base_y + 8],
                         12, fill="#a8a8ae")
    sh = Image.new("RGBA", (pf.width, 132), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([pf.width * 0.06, 0, pf.width * 0.94, 88],
                               fill=(20, 20, 24, 80))
    sh = sh.filter(ImageFilter.GaussianBlur(24))
    canvas.alpha_composite(sh, (px, base_y - 42))
    canvas.alpha_composite(pf, (px, py))

    # ---------- 5. 底部价格块 ----------
    blk_w, blk_h = 1330, 197
    bx0, by0 = (W - blk_w) // 2, 1732
    dr.rounded_rectangle([bx0, by0, bx0 + blk_w, by0 + blk_h], 38, fill=DARK_BAR)

    ptxt = format(int(price), ",") if price else "--"
    f_yen = core.font(49, True)
    f_num = core.font(118, True)
    sep = 46
    parts = [("¥", f_yen, DARK_WHITE, 16), (ptxt, f_num, DARK_WHITE, 0)]
    if has_cmp:
        parts += [("¥%s" % format(int(official), ","), core.font(47, True),
                   PINK_LINE, 8),
                  ("捡漏！", core.font(51, True), YELLOW_GO, 8)]

    total = sum(dr.textlength(t, font=f) for t, f, _, _ in parts) + sep * (len(parts) - 1)
    gx = bx0 + (blk_w - total) // 2
    cyy = by0 + blk_h // 2
    strike = None
    for t, f, col, dy in parts:
        w = dr.textlength(t, font=f)
        dr.text((gx, cyy + dy), t, font=f, fill=col, anchor="lm")
        if has_cmp and t.startswith("¥") and t != "¥":
            strike = (gx, cyy + dy, gx + w)
        gx += w + sep
    if strike:
        dr.line([strike[0], strike[1], strike[2], strike[1]],
                fill=PINK_LINE, width=3)

    # ---------- 6. 最底部配送说明 ----------
    ship_txt = "全新未拆封·" + SHIP_TEXT.get(ship, SHIP_TEXT["N"])
    dr.text((W // 2, 1990), ship_txt, font=core.font(36, False),
            fill=DARK_DIM, anchor="mm")

    out = core.load_rgb_img(canvas)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    out.save(out_path, quality=96)
    return out_path

