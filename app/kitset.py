# -*- coding: utf-8 -*-
"""kitset.py — 蓝底套图（2026-10-21 用户定稿，完全独立的第三样东西）
跟「写文案」(api_get /copy) 和婷宝「配图」(tbyp) 零关系、零共用窗口。
能力 = kit_set_demo/_make_set.py 的渲染链原样搬入（用户手搓定稿的版式）：
  四张 1080×1080：①主图(两颗蓝胶囊+底部卖点行) ②核心参数 ③官方卖点2×2 ④产品尺寸
规矩（preview.html 规矩栏）：图上无价格；首句两颗胶囊=品牌+品类+容量 / 特点+型号；
标题三档=全新未拆/样机清仓/特价处理（品牌+卖点+型号）；文案=卖点一话一行+售后一句。
数据：参数/机图复用现有抓取链（pubwork 爬虫 → 官网三站 → 欣盛），AI 不参与首版。
"""
import colorsys
import glob
import io
import json
import os
import re
import urllib.parse
from collections import Counter

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageChops

from copywriter import copy_root, fetch_official, fetch_xinsheng, fetch
from normalize import norm_model
from paths import wp
import pei_core
from net import urlopen_ssl

if os.name == "nt":
    F_LIGHT = r"C:\Windows\Fonts\msyhl.ttc"
    F_REG = r"C:\Windows\Fonts\msyh.ttc"
    F_BOLD = r"C:\Windows\Fonts\msyhbd.ttc"
else:
    # macOS（2026-10-25 实锤：硬编码 C:\Windows\Fonts 在 Mac 上第一行就炸）
    # 苹方 PingFang.ttc 的 weight index 各版本系统不一致，用 STHeiti 双文件最稳：
    F_LIGHT = "/System/Library/Fonts/STHeiti Light.ttc"
    F_REG = "/System/Library/Fonts/STHeiti Medium.ttc"
    F_BOLD = "/System/Library/Fonts/STHeiti Medium.ttc"
BLUE = (46, 107, 230)
INK = (28, 30, 34)
GRAY = (138, 142, 150)
LGRAY = (196, 200, 206)
LINE = (236, 238, 240)
PILL_BG = (233, 241, 253)
PILL_FG = (24, 74, 160)

_SKIP_KEYS = ("箱门结构", "面板材质", "控温方式", "额定电压", "额定频率",
              "搁物架", "果菜盒", "变温抽屉", "瓶座", "冷冻抽屉", "速冷速冻",
              "童锁", "包装尺寸", "上市时间", "型号", "毛重", "净重")


def kit_root():
    return wp("kitset")


def model_dir(model):
    nm = norm_model(model) or re.sub(r"\s+", "", model)
    return os.path.join(kit_root(), nm)


def _font(p, s):
    return ImageFont.truetype(p, s)


def _tw(d, s, f):
    b = d.textbbox((0, 0), s, font=f)
    return b[2] - b[0]


def _wrap(d, s, f, maxw):
    """按像素宽折行（卖点卡文字用）。"""
    line, out = "", []
    for ch_ in s:
        if line and _tw(d, line + ch_, f) > maxw:
            out.append(line)
            line = ch_
        else:
            line += ch_
    if line:
        out.append(line)
    return out


def _spaced(d, xy, s, f, fill, tracking=8):
    x, y = xy
    for ch in s:
        d.text((x, y), ch, font=f, fill=fill)
        x += _tw(d, ch, f) + tracking


def _pill(d, cx_right, y, s, f, pad=14, h=36):
    w = _tw(d, s, f) + pad * 2
    d.rounded_rectangle([cx_right - w, y, cx_right, y + h], radius=h // 2, fill=PILL_BG)
    d.text((cx_right - w + pad, y + h // 2), s, font=f, fill=PILL_FG, anchor="lm")
    return w


def _pill_left(d, x, y, s, f, pad=14, h=36):
    w = _tw(d, s, f) + pad * 2
    d.rounded_rectangle([x, y, x + w, y + h], radius=h // 2, fill=PILL_BG)
    d.text((x + pad, y + h // 2), s, font=f, fill=PILL_FG, anchor="lm")
    return w


def _pill_center(od, cx, y, s, f, pad_x=34, pad_y=20):
    w = _tw(od, s, f) + pad_x * 2
    asc, desc = f.getmetrics()
    h = asc + desc + pad_y * 2
    od.rounded_rectangle([cx - w / 2, y, cx + w / 2, y + h], radius=h / 2, fill=(46, 107, 230, 242))
    od.text((cx, y + h / 2), s, font=f, fill=(255, 255, 255), anchor="mm")
    return h


def _white_to_alpha(im, hard=242, soft=225):
    im = im.convert("RGBA")
    data = []
    for r, g, b, a in im.getdata():
        m = min(r, g, b)
        if m >= hard:
            a = 0
        elif m >= soft:
            a = int(255 * (hard - m) / (hard - soft))
        data.append((r, g, b, a))
    im.putdata(data)
    return im


def _flat_white(im):
    """RGBA 垫白合成后转 RGB。官方尺寸线稿 PNG 的透明区下面藏着紫色隐藏填充层
    （设计导出惯例），直接 convert('RGB') 会把隐藏填充全暴露——线稿变实心紫块、
    文字后面垫出黑框（251WBIU1/400WDCTU1 双实锤）。外部进来的图一律先过这里。"""
    if im.mode == "RGB":
        return im
    rgba = im.convert("RGBA")
    bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    return Image.alpha_composite(bg, rgba).convert("RGB")


def _autocrop_white(im, margin=16):
    from PIL import ImageChops
    im = _flat_white(im)
    bg = Image.new("RGB", im.size, (255, 255, 255))
    diff = ImageChops.difference(im.convert("RGB"), bg)
    box = diff.getbbox()
    if not box:
        return im
    l, t, r, b = box
    l = max(0, l - margin); t = max(0, t - margin)
    r = min(im.size[0], r + margin); b = min(im.size[1], b + margin)
    return im.crop((l, t, r, b))


def _crop_prod(img):
    """机图裁边成紧贴内容的 RGBA（透明 bbox / 白底转 alpha 再裁）。
    img1/img2 的定位预算必须用这份裁后尺寸——原图带透明边距时按原图算预算高偏小，
    机器实际贴出来上沿顶进「核心参数」标题（2026-10-28 实锤）。"""
    rgba = img.convert("RGBA")
    alo, _ = rgba.split()[3].getextrema()
    if alo < 250:
        out = rgba.crop(rgba.split()[3].getbbox())
    else:
        out = _white_to_alpha(_autocrop_white(rgba.convert("RGB")))
        bbox = out.split()[3].getbbox()
        if bbox:
            out = out.crop(bbox)
    return out


def _paste_product(C, img, cx, bottom_y, target_h, max_w=1080):
    img = _crop_prod(img)
    w, h = img.size
    if h < 10:
        return
    # 高度卡 target_h、宽度卡 max_w（电视等 16:9 横图按高放会糊满整卡，2026-10-22 实锤）
    sc = min(target_h / float(h), max_w / float(w))
    im2 = img.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.LANCZOS)
    C.paste(im2, (int(cx - im2.size[0] / 2), int(bottom_y - im2.size[1])), im2)


def _place(th, bottom0, top_min=230, keep_gap=120):
    top = bottom0 - th
    if top - top_min > keep_gap:
        return (top_min + bottom0 + th) // 2, True
    return bottom0, False


def _header(d, cn):
    d.rectangle([60, 92, 132, 94], fill=BLUE)
    _spaced(d, (60, 116), cn, _font(F_REG, 34), INK, tracking=10)


def _render_img1(cfg, prod, out):
    C = Image.new("RGB", (1080, 1080), (255, 255, 255))
    d = ImageDraw.Draw(C)
    _spaced(d, (60, 88), cfg.get("brand_en", "HAIER"), _font(F_LIGHT, 30), INK, tracking=14)
    _spaced(d, (60, 132), cfg["series"], _font(F_LIGHT, 24), GRAY, tracking=8)
    d.rectangle([60, 186, 108, 189], fill=BLUE)
    th = cfg.get("img1_h", 860)
    # 电视等 16:9 横图实际渲染高远小于 th，按 th 定位会沉底/偏移——用实际高定位（2026-10-22）
    # 大方块（洗碗机等高宽都大的深色机身）再整体缩到 680 高，别顶页眉别压字（CWY15 实锤）
    # 预算用裁完边的内容尺寸（2026-10-28）：原图透明边距会让预算高偏小、机器顶到页眉
    prod = _crop_prod(prod)
    try:
        _pw, _ph = prod.size
        sc = min(th / float(_ph), 960.0 / _pw)
        h_act = _ph * sc
        if h_act > 680 and _pw * sc > 640:
            h_act = 680.0
    except Exception:
        h_act = th
    bottom, centered = _place(h_act, 916)
    d.ellipse([540 - 280, bottom - 28, 540 + 280, bottom + 2], fill=(238, 240, 243))
    _paste_product(C, prod, 540, bottom, h_act, max_w=960)
    ov = Image.new("RGBA", C.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    f1, f2 = _font(F_BOLD, 58), _font(F_BOLD, 50)
    def _ph(f):
        a, dsc = f.getmetrics()
        return a + dsc + 40
    # 用户令（2026-10-24）：型号必须单独一颗胶囊（只要是带型号的句子，一律拆出来），
    # 前面文字已在 _split_name 按语义断好；没走 _split_name 的也在这里剥型号
    lines = [cfg["line1"], cfg["line2"]]
    _m = cfg.get("model") or ""
    if _m and lines[1].endswith(_m) and len(lines[1]) > len(_m):
        _body = lines[1][:-len(_m)]
        lines = [lines[0], _body, _m] if _body else [lines[0], _m]
    elif _tw(od, lines[1], f2) + 68 > 1000:
        # 不含型号却超宽的老兜底：对半拆（走到这里说明型号已不在尾部）
        half = len(lines[1]) // 2
        lines = [lines[0], lines[1][:half], lines[1][half:]]
    y0 = 420
    total_h = sum(_ph(f1 if i == 0 else f2) for i in range(len(lines))) + 18 * (len(lines) - 1)
    if centered:
        y0 = int(bottom - h_act / 2 - total_h / 2)
    for i, s_ in enumerate(lines):
        h = _pill_center(od, 540, y0, s_, f1 if i == 0 else f2)
        y0 += h + 18
    C = Image.alpha_composite(C.convert("RGBA"), ov).convert("RGB")
    d = ImageDraw.Draw(C)
    d.text((540, 962), cfg["features_line"], font=_font(F_REG, 31), fill=INK, anchor="ma")
    C.save(out, quality=95)


def _render_img2(cfg, prod, out):
    C = Image.new("RGB", (1080, 1080), (255, 255, 255))
    d = ImageDraw.Draw(C)
    _header(d, "核 心 参 数")
    th2 = cfg.get("img2_h", 640)
    # 定位预算用裁完边的内容尺寸（2026-10-28 实锤）：原图透明边距大时预算高偏小，
    # 机器实际贴出来上沿顶进「核心参数」标题——先裁再算，所见即所得
    prod = _crop_prod(prod)
    try:
        _pw2, _ph2 = prod.size
        h2_act = _ph2 * min(th2 / float(_ph2), 440.0 / _pw2)
    except Exception:
        h2_act = th2
    bottom2, _ = _place(h2_act, 850, top_min=250)
    d.ellipse([270 - 150, bottom2 - 24, 270 + 150, bottom2 + 6], fill=(243, 244, 246))
    _paste_product(C, prod, 270, bottom2, th2, max_w=440)
    x0, y0, cw = 520, 246, 500
    for i, (k, v) in enumerate(cfg["params_rows"]):
        y = y0 + i * 74
        d.text((x0, y + 26), k, font=_font(F_LIGHT, 26), fill=GRAY)
        _pill(d, x0 + cw, y + 9, v, _font(F_REG, 23))
        d.rectangle([x0, y + 62, x0 + cw, y + 63], fill=LINE)
    d.text((60, 900), cfg["size_label"], font=_font(F_LIGHT, 27), fill=GRAY)
    sp2 = cfg.get("size_pills") or []
    if sp2:
        w2 = _pill(d, 1020, 894, sp2[1], _font(F_REG, 23), h=42)
        _pill(d, 1020 - w2 - 12, 894, sp2[0], _font(F_REG, 23), h=42)
    else:
        _pill(d, 1020, 894, "以实物为准", _font(F_REG, 23), h=42)
    d.rectangle([60, 960, 1020, 961], fill=LINE)
    C.save(out, quality=95)


def _serif(sz, bold=True):
    """V8 卖点图衬线数字/英文（Times）；找不到就退回黑体（Mac 兼容）。"""
    name = "timesbd.ttf" if bold else "times.ttf"
    for d in (os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
              "/System/Library/Fonts/Supplemental", "/Library/Fonts"):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return ImageFont.truetype(p, sz)
    try:
        return ImageFont.truetype("/System/Library/Fonts/Supplemental/Times New Roman%s.ttf" % (" Bold" if bold else ""), sz)
    except Exception:
        return _font(F_BOLD if bold else F_REG, sz)


def _v8_points(points, params_rows, n=6):
    """V8 卖点池（2026-10-28 用户拍板）：话术卖点去重（kg/公斤归一）+ 官网参数补足到 6 条。
    参数条以「k：v」入池，渲染时值做主词、标签做灰小字。只影响卖点图，文案区不受影响。"""
    out, seen = [], set()
    for t in (points or []):
        t = str(t).strip()
        if not t:
            continue
        head = (t.split("，")[0] or t.split(",")[0]).strip()
        key = head.replace("kg", "公斤").replace("KG", "公斤").replace("Kg", "公斤").lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    for k, v in (params_rows or []):
        if len(out) >= n:
            break
        t = ("%s：%s" % (k, v)).strip()
        if v and t not in out:
            out.append(t)
    return out[:n]


def _render_img3(cfg, points, out):
    """卖点图 V8 非对称编辑排版（2026-10-28 用户拍板定稿，样张 _kit_demo 卖点_V8）：
    左栏品牌头+金色短竖线+竖排「官方卖点」+金色字距 HIGHLIGHTS+底部型号；
    右栏 6 条等分：上行粗黑主词+下行灰小字说明（参数条值做主词），
    金衬线序号 01-06 + 细分隔线。白底。"""
    C = Image.new("RGB", (1080, 1080), (255, 255, 255))
    d = ImageDraw.Draw(C)
    d.text((66, 92), " ".join(cfg.get("brand_en") or "HAIER"), font=_font(F_BOLD, 26), fill=(150, 158, 168), anchor="lm")
    d.text((66, 132), (cfg.get("brand") or "") + (cfg.get("cat") or ""), font=_font(F_BOLD, 26), fill=(90, 98, 108), anchor="lm")
    d.rounded_rectangle([66, 158, 116, 163], 2, fill=BLUE)
    vt = "官方卖点"
    vy0 = 264
    d.rounded_rectangle([100, vy0 + 10, 104, vy0 + 10 + 130], 2, fill=(201, 168, 102))
    for j, chch in enumerate(vt):
        d.text((166, vy0 + j * 122), chch, font=_font(F_BOLD, 84), fill=INK, anchor="mm")
    d.text((166, vy0 + len(vt) * 122 + 34), "H I G H L I G H T S", font=_serif(18, False), fill=(201, 168, 102), anchor="mm")
    d.text((66, 1020), cfg.get("model") or "", font=_font(F_REG, 20), fill=(190, 196, 204), anchor="lm")
    d.line([262, 96, 262, 1006], fill=(232, 236, 242), width=1)
    pts = _v8_points(points, cfg.get("params_rows"))
    if not pts:
        C.save(out, quality=95)
        return
    rx = 316
    y0, y1 = 132, 992
    m = len(pts)
    rh = (y1 - y0) / float(m)
    for i, t in enumerate(pts):
        if "：" in t or ":" in t:
            # 参数条：值为主词（粗黑），标签为说明（灰小）
            k, _, v = t.replace("：", ":").partition(":")
            main, sub = v.strip(), k.strip()
        else:
            seg = t.replace("，", "\x01").replace(",", "\x01").split("\x01")
            main, sub = seg[0].strip(), (seg[1].strip() if len(seg) > 1 else "")
        yy = y0 + rh * (i + 0.5)
        d.text((rx, yy - 14), "%02d" % (i + 1), font=_serif(30), fill=(201, 168, 102), anchor="lm")
        fs = 36
        while fs > 22 and _tw(d, main, _font(F_BOLD, fs)) > 1080 - rx - 230:
            fs -= 2
        d.text((rx + 58, yy - 14), main, font=_font(F_BOLD, fs), fill=INK, anchor="lm")
        if sub:
            d.text((rx + 58, yy + 22), sub, font=_font(F_BOLD, 20), fill=GRAY, anchor="lm")
        if i < m - 1:
            d.line([rx, y0 + rh * (i + 1), 1080 - 72, y0 + rh * (i + 1)], fill=(238, 240, 244), width=1)
    C.save(out, quality=95)


def _label_size_drawing(im2, wmm, hmm, dmm):
    """官方尺寸图没印数字时，把宽/高/深按官方样式标到对应视图旁。
    规矩（2026-10-22 用户令）：官方线稿线条/结构/标注位置原样保留，我们只在
    缺数字的地方补数字；官网图裁完白边后四周没地方写字 → 自动向左/向下扩白垫边。
    图上已印数字的（251WBIU1 线稿自带 1770/558/540）逐项检测跳过不重复标。"""
    im = _flat_white(im2)
    w, h = im.size
    px = im.load()
    def purple(c):
        r, g, b = c
        return b > 100 and b - g > 40 and r < b
    cols = [sum(1 for y in range(h) if purple(px[x, y])) for x in range(w)]
    segs, s = [], None
    for x in range(w + 1):
        c = cols[x] if x < w else 0
        if c > 4 and s is None:
            s = x
        if c <= 4 and s is not None:
            segs.append((s, x))
            s = None
    segs = [(a, b) for a, b in segs if b - a >= 12]
    if len(segs) < 2:
        return im
    front = max(segs, key=lambda t: t[1] - t[0])
    side = max([t for t in segs if t != front], key=lambda t: t[1] - t[0])
    def vrange(x0, x1):
        ys = [y for y in range(h) for x in range(x0, x1, 2) if purple(px[x, y])]
        return (min(ys), max(ys)) if ys else None
    fr = vrange(*front)
    sr = vrange(*side) if side else None
    if not fr:
        return im
    fsize = max(22, int(h * 0.05))
    font = ImageFont.truetype(F_BOLD, fsize)
    pc = px[(front[0] + front[1]) // 2, (fr[0] + fr[1]) // 2]
    def band_ink(x0, y0, x1, y1):
        x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(w, int(x1)), min(h, int(y1))
        n = 0
        for y in range(y0, y1, 2):
            for x in range(x0, x1, 2):
                if purple(px[x, y]):
                    n += 1
        return n > 12
    def band_rows(x0, y0, x1, y1):
        """带内含紫像素的行占比：印着数字的带是稀疏文字（20%~80% 行有墨），
        光杆尺寸线/实心装饰条是整段贯通（>80%）或全空——后者才需要补数字。"""
        x0, y0, x1, y1 = max(0, int(x0)), max(0, int(y0)), min(w, int(x1)), min(h, int(y1))
        if y1 <= y0:
            return 0.0
        n = 0
        for y in range(y0, y1):
            for x in range(x0, x1):
                if purple(px[x, y]):
                    n += 1
                    break
        return n / float(y1 - y0)
    plan = []
    # 宽度：正视图下方
    if not band_ink(front[0], fr[1] + 4, front[1], fr[1] + int(fsize * 1.6)):
        plan.append(("w", (front[0] + front[1]) // 2, fr[1], wmm))
    # 深度：侧视图下方
    if sr and not band_ink(side[0], sr[1] + 4, side[1], sr[1] + int(fsize * 1.6)):
        plan.append(("d", (side[0] + side[1]) // 2, sr[1], dmm))
    # 高度：正视图左侧。已印数字（稀疏文字行）→ 跳过；光杆线/空白 → 补数字
    rr = band_rows(front[0] - int(fsize * 2.6), fr[0], front[0] - 4, fr[1])
    has_h_num = 0.12 < rr < 0.8
    hi_w = int(fsize * 1.5)
    minx = w
    for x in range(w):
        if any(purple(px[x, y]) for y in range(fr[0], fr[1], 3)):
            minx = x
            break
    lp = 0
    if not has_h_num:
        plan.append(("h", 0, (fr[0] + fr[1]) // 2, hmm))
        if minx - hi_w - 10 < 0:
            lp = hi_w + 12 - minx   # 左侧没空间 → 扩白垫边
    if not plan:
        return im
    strip = int(fsize * 1.9) if any(p[0] in ("w", "d") for p in plan) else 0
    if lp or strip:
        big = Image.new("RGB", (w + lp, h + strip), (255, 255, 255))
        big.paste(im, (lp, 0))
        im = big
    d = ImageDraw.Draw(im)
    for kind, cx, ybot, val in plan:
        if kind in ("w", "d"):
            d.text((lp + cx, ybot + 6), "%dmm" % val, font=font, fill=pc, anchor="ma")
    if not has_h_num:
        hi = Image.new("RGBA", (int(fsize * 5.5), hi_w), (0, 0, 0, 0))
        dh = ImageDraw.Draw(hi)
        dh.text((hi.size[0] // 2, hi.size[1] // 2), "%dmm" % hmm, font=font, fill=pc, anchor="mm")
        hi = hi.rotate(90, expand=True)
        im.paste(hi, (lp + minx - hi.size[0] - 10, (fr[0] + fr[1]) // 2 - hi.size[1] // 2), hi)
    return im


def _sketch_rgba(path, color=(28, 30, 34)):
    """机图 → 线稿 RGBA（2026-10-28 用户令「没有尺寸的就画一个」）：
    透明抠图 = alpha 轮廓实边 + 实心区内部灰阶边缘（弱化 45%）当细节线；
    白底图 = 整图 FIND_EDGES。只描形，绝不编尺寸数字。失败返回 None。
    color=线条颜色：聚光深底传浅色 (238,242,248)，蓝底白底传深色 (28,30,34)。"""
    try:
        im = Image.open(path).convert("RGBA")
        a = im.getchannel("A")
        lo, _ = a.getextrema()
        if lo < 245:
            bb = a.getbbox()
            if bb:
                im = im.crop(bb)
                a = im.getchannel("A")
            er = a.filter(ImageFilter.MinFilter(5))
            outline = ImageChops.subtract(a, er)
            rgb = Image.alpha_composite(Image.new("RGBA", im.size, (255, 255, 255, 255)), im).convert("L")
            ie = rgb.filter(ImageFilter.FIND_EDGES).point(lambda v: min(255, int(v * 1.6)))
            ie = ImageChops.multiply(ie, er)              # 只留机器内部的细节边
            alpha = ImageChops.lighter(outline, ie.point(lambda v: v * 45 // 100))
        else:
            rgb = im.convert("L")
            bb = Image.eval(rgb, lambda v: 255 - v).getbbox()
            if bb:
                rgb = rgb.crop(bb)
            alpha = rgb.filter(ImageFilter.FIND_EDGES).point(lambda v: min(255, int(v * 1.8)))
        sk = Image.new("RGBA", alpha.size, tuple(color) + (0,))
        sk.putalpha(alpha)
        return sk
    except Exception:
        return None


def _render_img4(cfg, out, prod=None):
    C = Image.new("RGB", (1080, 1080), (255, 255, 255))
    d = ImageDraw.Draw(C)
    if not cfg.get("sizeimg"):
        # 无官方线稿的两个兜底分支（手绘线框/机图线稿）也按聚光版式：标题居中+金短线
        d.text((540, 96), "产 品 尺 寸", font=_font(F_BOLD, 50), fill=INK, anchor="mm")
        d.rounded_rectangle([500, 136, 580, 142], 3, fill=(212, 175, 108))
    if cfg.get("sizeimg"):
        # 纯白版式（2026-10-28 用户拍板样张）：顶部「产品尺寸」+金线，官方线稿
        # 840×620 双限居中，底部品牌型号 + 宽高深一行——「同时支持」胶囊带删除
        d.text((540, 96), "产 品 尺 寸", font=_font(F_BOLD, 50), fill=INK, anchor="mm")
        d.rounded_rectangle([500, 136, 580, 142], 3, fill=(212, 175, 108))
        simg = _rescue_prod(_autocrop_white(_flat_white(Image.open(cfg["sizeimg"])), margin=6), None)
        w, h = simg.size
        sc = min(840.0 / w, 620.0 / h)
        im2 = simg.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.LANCZOS)
        im2 = im2.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))
        sp = cfg.get("size_pills") or []
        nums = re.findall(r"\d+", " ".join(sp))
        if len(nums) >= 3:
            # 官方图没印数字时，把真实尺寸标到视图旁（已有数字的自动跳过）
            try:
                im3 = _label_size_drawing(im2, int(nums[0]), int(nums[1]), int(nums[2]))
                if im3.size != im2.size:
                    # 标注函数为写字垫了白边 → 整体缩回预算内
                    sc3 = min(840.0 / im3.size[0], 620.0 / im3.size[1])
                    im2 = im3.resize((max(1, int(im3.size[0] * sc3)), max(1, int(im3.size[1] * sc3))), Image.LANCZOS)
                else:
                    im2 = im3
            except Exception:
                pass
        C.paste(im2, (int(540 - im2.size[0] / 2), 180 + max(0, int((640 - im2.size[1]) / 2))))
        # 底部两行：品牌+型号 / 宽高深（无实测数字就不硬标）
        d.text((540, 918), "%s  %s" % (cfg.get("brand", ""), cfg.get("model", "")),
               font=_font(F_BOLD, 30), fill=INK, anchor="mm")
        dims = cfg.get("dims") or ([int(n) for n in nums[:3]] if len(nums) >= 3 else None)
        if dims and all(dims):
            d.text((540, 972), "宽 %d mm · 高 %d mm · 深 %d mm" % tuple(dims),
                   font=_font(F_REG, 25), fill=GRAY, anchor="mm")
        C.save(out, quality=95)
        return
    if cfg.get("dims"):
        W, H, D = cfg["dims"]
        fh = cfg.get("draw_fh", 460); fw = max(60, int(W / H * fh))
        fx, fy = 240, 300
        d.rounded_rectangle([fx, fy, fx + fw, fy + fh], radius=10, outline=INK, width=2)
        d.line([fx + 14, fy + fh - 44, fx + fw - 14, fy + fh - 44], fill=GRAY, width=1)
        def dim_line(x1, y1, x2, y2, label, vertical=False):
            d.line([x1, y1, x2, y2], fill=GRAY, width=2)
            if vertical:
                d.line([x1 - 8, y1, x1 + 8, y1], fill=GRAY, width=2)
                d.line([x2 - 8, y2, x2 + 8, y2], fill=GRAY, width=2)
                d.text((x1 + 16, (y1 + y2) // 2), label, font=_font(F_REG, 26), fill=INK, anchor="lm")
            else:
                d.line([x1, y1 - 8, x1, y1 + 8], fill=GRAY, width=2)
                d.line([x2, y2 - 8, x2, y2 + 8], fill=GRAY, width=2)
                d.text(((x1 + x2) // 2, y1 - 18), label, font=_font(F_REG, 26), fill=INK, anchor="mb")
        dim_line(fx, fy - 44, fx + fw, fy - 44, str(W))
        dim_line(fx + fw + 40, fy, fx + fw + 40, fy + fh, str(H), vertical=True)
        d.text((fx + fw // 2, fy + fh + 40), "正视图", font=_font(F_LIGHT, 22), fill=LGRAY, anchor="ma")
        if fx + fw + 40 + 240 + 60 > 1020:
            pw = min(fw, 560); px, py = fx, fy + fh + 170
            ph = max(10, int(D / W * pw))
            d.rounded_rectangle([px, py, px + pw, py + ph], radius=8, outline=INK, width=2)
            dim_line(px, py - 44, px + pw, py - 44, str(W))
            dim_line(px + pw + 40, py, px + pw + 40, py + ph, str(D), vertical=True)
            d.text((px + pw // 2, py + ph + 14), "俯视图", font=_font(F_LIGHT, 22), fill=LGRAY, anchor="ma")
        else:
            pw = 240; ph = int(D / W * pw)
            px, py = 680, 330
            d.rounded_rectangle([px, py, px + pw, py + ph], radius=8, outline=INK, width=2)
            dim_line(px, py - 44, px + pw, py - 44, str(W))
            dim_line(px + pw + 40, py, px + pw + 40, py + ph, str(D), vertical=True)
            d.text((px + pw // 2, py + ph + 40), "俯视图", font=_font(F_LIGHT, 22), fill=LGRAY, anchor="ma")
    else:
        # 无官方尺寸图、参数里也没真实尺寸（2026-10-28 用户令「没有尺寸的就画一个」）：
        # 机图描深色线稿占主位（版式同聚光、纯白底），绝不编数字，底部一句话兜底
        sk = _sketch_rgba(prod, color=(28, 30, 34)) if prod else None
        if sk is not None:
            sc4 = min(840.0 / max(1, sk.width), 620.0 / max(1, sk.height))
            tw4 = max(1, int(sk.width * sc4))
            th4 = max(1, int(sk.height * sc4))
            sk = sk.resize((tw4, th4), Image.LANCZOS)
            C.paste(sk, (int(540 - tw4 / 2), 180 + max(0, int((640 - th4) / 2))), sk)
        d.text((540, 918), "%s  %s" % (cfg.get("brand", ""), cfg.get("model", "")),
               font=_font(F_BOLD, 30), fill=INK, anchor="mm")
        d.text((540, 972), "外形尺寸以实物为准 · 可咨询客服获取详细规格",
               font=_font(F_REG, 25), fill=GRAY, anchor="mm")
    C.save(out, quality=95)


# ---------------- 数据组装（参数/机图走现有抓取链） ----------------

def _http_text(url, referer=None):
    """中关村专用取页：ZOL 是 GB18030 编码，utf-8 直解会乱码。"""
    import gzip as _gz
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120",
               "Accept-Encoding": "gzip"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urlopen_ssl(req, timeout=25) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = _gz.GzipFile(fileobj=io.BytesIO(data)).read()
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", "replace")


def _zol_data(model, brand, log):
    """中关村在线兜底（下架/冷门型号官网欣盛都无图无参时用）。返回 (图片URL列表, 参数kv列表)。
    直连搜索会被反爬挡（返回空页），所以走两路：直搜 → 必应中转搜 detail.zol.com.cn 详情页。
    搜索/详情页都有瞬时反爬抖动（2026-10-29 实锤：同一型号上一把 0/0、下一把 13 条参数），
    空手时歇 2 秒再试一次。"""
    import time as _t
    for attempt in (0, 1):
        imgs, props = _zol_data_once(model, brand, log if attempt == 0 else None)
        if imgs or props:
            return imgs, props
        if attempt == 0:
            _t.sleep(2)
    return [], []


def _zol_data_once(model, brand, log):
    pages = []
    try:
        q = urllib.parse.quote(("%s%s" % (brand or "", model)).strip())
        html = _http_text("https://detail.zol.com.cn/index.php?c=SearchList&keyword=" + q)
        if html:
            pages += [u for u, t in re.findall(
                r'href="(https://detail\.zol\.com\.cn/[^"]+?/[^"]+?\.shtml)"[^>]*>([^<]{4,60})<', html)
                if (norm_model(model) or "")[:8] in norm_model(t)]
    except Exception:
        pass
    if not pages:
        # 必应 RSS 抽风式限流（2026-10-29 实锤：同一查询一把有结果一把空频道）——
        # www/cn 两个入口 + count=30 变体连试，谁出结果用谁
        try:
            import time as _t2
            base_q = "site:detail.zol.com.cn " + model
            for bu in ("https://www.bing.com/search?" + urllib.parse.urlencode({"q": base_q, "format": "rss"}),
                       "https://cn.bing.com/search?" + urllib.parse.urlencode({"q": base_q, "format": "rss"}),
                       "https://www.bing.com/search?" + urllib.parse.urlencode({"q": base_q, "format": "rss", "count": "30"})):
                xml = _http_text(bu)
                pages += re.findall(r"<link>(https://detail\.zol\.com\.cn/[^<]+?\.shtml)</link>", xml or "")
                if pages:
                    break
                _t2.sleep(1)
        except Exception:
            pass
    pages = [p for p in dict.fromkeys(pages) if "/param." not in p][:2]
    if log:
        log("中关村候选详情页 %d 个" % len(pages))
    imgs, props = [], []
    for page in pages:
        try:
            html2 = _http_text(page, "https://www.bing.com/")
            for m in re.finditer(r'(https?://[^"\'\s]+?(?:zol-img\.com|zol\.com\.cn)/product/[^"\'\s]+?\.(?:jpg|png))', html2):
                u = re.sub(r"_\d+x\d+/", "_500x375/", m.group(1))   # 升到 500px 大图
                if u not in imgs and not re.search(r"logo|icon|banner|watermark", u, re.I):
                    imgs.append(u)
            # 参数子页：ZOL 两种结构都要吃——
            #   旧版 <p title="…"><span>键：</span>值</p>；
            #   现行表格 <th><span id="newPmName_N">键</span></th>
            #              <td><span id="newPmVal_N">值</span><em>纠错</em></td>
            # （2026-10-29 实锤：旧正则跟表格结构完全不匹配，ZOL 参数从来就没解析出来过）
            m = re.search(r'href="(/[^"]*?/param\.shtml)"', html2)
            if m:
                purl = "https://detail.zol.com.cn" + m.group(1)
                phtml = _http_text(purl, page)
                phtml = re.sub(r"<em[^>]*>.*?</em>", " ", phtml, flags=re.S)   # 去「纠错」
                for rx in (r'<p title="[^"]*"><span>\s*([^<：]+?)：?\s*</span>\s*([^<]{1,80}?)\s*</p>',
                           r'<th[^>]*>\s*<span[^>]*>\s*([^<：]{1,30}?)：?\s*</span>\s*</th>\s*'
                           r'<td[^>]*>\s*<span[^>]*>([^<]{1,80}?)\s*</span>'):
                    for km, vm in re.findall(rx, phtml):
                        k, v = km.strip(), vm.strip()
                        if k and v and v != "-" and (k, v) not in props:
                            props.append((k, v))
            if imgs or props:
                break
        except Exception:
            continue
    if log:
        log("中关村：%d 张图 / %d 条参数" % (len(imgs), len(props)))
    return imgs, props


def _gather_info(model, log):
    """pubwork 爬虫 → 官网三站 → 欣盛，三路凑 props/brand/title/price/url。
    返回 (info, data imgs_urls)；info 至少含 model/brand/title/props。"""
    info = None
    try:
        import pubwork
        info = pubwork.load_info(model)
        if not info or not (info.get("props") or []):
            info = pubwork.work(model, prog=lambda p, m: (log(m) if log else None))
    except Exception as e:
        if log:
            log("pubwork 未命中（%s）" % e)
        info = None
    imgs = []
    data = None
    try:
        data = fetch_official(model, log)
    except Exception:
        data = None
    xs = None
    try:
        xs = fetch_xinsheng(model, log)
    except Exception:
        xs = None
    for src in (data, xs):
        if src and src.get("imgs"):
            imgs.extend([u for u in src["imgs"] if u])
    if imgs:
        # 大图变体优先（_1200 > 其他 > _600）：透明底机图锁定不比面积，先到先得，
        # 600px 小图先到会顶掉 1200px 大图导致成图发糊（2026-10-22 XQS100 用户实锤）
        imgs = sorted(imgs, key=lambda u: 0 if "_1200" in u else (2 if "_600" in u else 1))
    if not info:
        info = {}
    if not (info.get("props") or []):
        props = []
        for src in (data, xs):
            for _g, kv in ((src or {}).get("groups") or []):
                for k, v in (kv or []):
                    if str(k) and str(v) and (str(k), str(v)) not in props:
                        props.append((str(k), str(v)))
        if props:
            info["props"] = props
    # 官网参数并集（2026-10-28 用户令）：pubwork/欣盛先到的 props 常缺「尺寸」键
    # （EB100Z35Pro2 官网明摆着 尺寸(宽*高*深)mm=550*1030*570 却没进 props 实锤）——
    # 官网命中的键追加到 props 尾部、不动已有顺序；_parse_dims 按键名找尺寸，补上即出真尺寸
    if (data or {}).get("groups") and info.get("props") is not None:
        _have = {str(k).strip() for k, _ in (info.get("props") or [])}
        for _g, kv in (data.get("groups") or []):
            for k, v in (kv or []):
                ks, vs = str(k).strip(), str(v).strip()
                if ks and vs and ks not in _have:
                    info["props"].append((ks, vs))
                    _have.add(ks)
    if not (info.get("title") or ""):
        for src in (data, xs):
            if src and src.get("pname"):
                info["title"] = str(src["pname"])
                break
    if not (info.get("brand") or ""):
        t = (info.get("title") or "") + " " + model
        info["brand"] = "卡萨帝" if "卡萨帝" in t else ("统帅" if "统帅" in t or "Leader" in t else "海尔")
    info.setdefault("model", norm_model(model) or model)
    if info.get("url"):
        try:
            import pei_core as _pc
            _c = _pc.cat_from_url(info["url"])
            if _c:
                info["cat"] = _c  # 缓存里的 cat 可能是旧映射（HKC3000 净水机被标洗衣机实锤），按 URL 现算覆盖
        except Exception:
            pass
    return info, imgs


def _cut_border_bg(im, thr=46):
    """边框同色判定 + 从边缘泛洪抠底：白底/黑底/灰底纯色背景的产品图 -> 透明底抠图。
    背景不统一（促销横幅/场景图/拼图）返回 None——这类图直接淘汰不用。"""
    from PIL import ImageDraw
    im = im.convert("RGBA")
    w, h = im.size
    if w < 200 or h < 200:
        return None
    px = im.load()
    border = []
    for x in range(0, w, max(1, w // 60)):
        border.append(px[x, 0][:3]); border.append(px[x, h - 1][:3])
    for y in range(0, h, max(1, h // 60)):
        border.append(px[0, y][:3]); border.append(px[w - 1, y][:3])
    border.sort()
    med = border[len(border) // 2]
    uniform = sum(1 for c in border
                  if abs(c[0] - med[0]) + abs(c[1] - med[1]) + abs(c[2] - med[2]) < 70) / float(len(border))
    if uniform < 0.85:
        return None
    seeds = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    seeds += [(x, 0) for x in range(w // 6, w, max(1, w // 6))]
    seeds += [(x, h - 1) for x in range(w // 6, w, max(1, w // 6))]
    seeds += [(0, y) for y in range(h // 4, h, max(1, h // 4))]
    seeds += [(w - 1, y) for y in range(h // 4, h, max(1, h // 4))]
    for s in seeds:
        try:
            ImageDraw.floodfill(im, s, (0, 0, 0, 0), thresh=thr)
        except Exception:
            pass
    bbox = im.split()[3].getbbox()
    if not bbox:
        return None
    frac = ((bbox[2] - bbox[0]) * (bbox[3] - bbox[1])) / float(w * h)
    if frac < 0.08 or frac > 0.92:
        return None
    bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if not (0.3 <= bh / float(bw) <= 5.0):
        return None
    # 底部落地比例：真产品照机器贴着底边（≥86%），悬浮创意图/泼墨图到不了
    return im.crop(bbox), bbox[3] / float(h), frac


def _max_dark_run(im):
    """最长连续暗色横程（200×200 缩图，lum<150）：线稿笔画细（语料 ≤14，带实心滚筒圆的
    XQG110=50 封顶）；照片实心块横贯（EB65 控制面板=86 / 京东横幅色块=91）。阈 60。"""
    g = _flat_white(im).convert("L").resize((200, 200))
    px = g.load()
    best = 0
    for y in range(200):
        run = 0
        for x in range(200):
            if px[x, y] < 150:
                run += 1
                if run > best:
                    best = run
            else:
                run = 0
    return best


def _interior_ink(im):
    """墨迹 bbox 内部（四边收缩15%）的非白占比：线稿空心（XQG110 带表格 hatch=0.221 封顶），
    产品照肚子里全是隔板/抽屉/酒水实心内容（536=0.277/216=0.310/EB65=0.534/220JE=0.838）。
    0.25 阈值在 12 台实测语料上完美二分（2026-10-22）。"""
    im2 = im.convert("RGB").resize((200, 200))
    px = im2.load()
    xs, ys = [], []
    for y in range(200):
        for x in range(200):
            r, g, b = px[x, y]
            if r < 215 and g < 215 and b < 215:
                xs.append(x)
                ys.append(y)
    if not xs:
        return 1.0
    x0, x1, y0, y1 = min(xs) + 15, max(xs) - 15, min(ys) + 15, max(ys) - 15
    if x1 <= x0 or y1 <= y0:
        return 1.0
    tot = ink = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b = px[x, y]
            tot += 1
            if r < 225 and g < 225 and b < 225:
                ink += 1
    return ink / float(tot)


def _ai_read_size_nums(path, log=None):
    """AI 视觉读官方尺寸图上印的数字 → (宽, 高, 深) 或 None。
    2026-10-26：参数表的「外形尺寸」常有横竖颠倒/包装尺寸混入（ES60H-Q3 参数
    755*440*415 vs 官方图实际印 750/433/405 实锤），官方图自己印的数字最权威。"""
    try:
        import base64
        import pei_core
        im = Image.open(path)
        im2 = _flat_white(im).convert("RGB")
        w, h = im2.size
        sc = 720.0 / max(w, h)
        if sc < 1:
            im2 = im2.resize((int(w * sc), int(h * sc)), Image.LANCZOS)
        buf = io.BytesIO()
        im2.save(buf, "JPEG", quality=85)
        data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        txt = pei_core._vision_call(None, data_url,
            "这是家电产品的尺寸线框图。读出图上标注的毫米数字：主视图的宽和高，"
            "以及俯视图/侧视图表示深度的那个数字。只回答一行 JSON："
            '{"w":数字,"h":数字,"d":数字}，某个读不出来就填 null，不要解释', log=log)
        if not txt:
            return None
        m = re.search(r'\{[^}]*\}', txt.replace("，", ","))
        if not m:
            return None
        j = json.loads(m.group(0))
        wv, hv, dv = (j.get("w"), j.get("h"), j.get("d"))
        vals = []
        for x in (wv, hv, dv):
            x = int(x) if x is not None and str(x).strip() != "null" else 0
            if not (80 <= x <= 3000):
                return None
            vals.append(x)
        return tuple(vals)
    except Exception:
        return None


def _ai_is_sizeimg(im, log=None):
    """AI 视觉认官方尺寸图（2026-10-26 用户令：「加了AI识别图的嘛，让他去找我要的图」）。
    线框启发式 _looks_like_sizeimg 漏判时（官网手绘风/异形版式）商汤→GLM 兜底认图。
    返回 True/False；AI 完全不可用返回 None（调用方据此放弃，别一张张空烧）。"""
    try:
        import base64
        import pei_core
        im2 = _flat_white(im).convert("RGB")
        w, h = im2.size
        sc = 640.0 / max(w, h)
        if sc < 1:
            im2 = im2.resize((int(w * sc), int(h * sc)), Image.LANCZOS)
        buf = io.BytesIO()
        im2.save(buf, "JPEG", quality=80)
        data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
        txt = pei_core._vision_call(None, data_url,
            "这张图是不是家电产品的官方尺寸示意图（线框图/三视图，带宽高深毫米数字标注）？"
            "产品照片、场景图、促销图都算不是。只回答：是 或 否", log=log)
        if not txt:
            return None
        t = txt.strip()[:8].lower()
        if "是" in txt[:8] or "yes" in t:
            return True
        if "否" in txt[:8] or "not" in t or "no" in t:
            return False
        return None
    except Exception:
        return None


def _looks_like_sizeimg(im):
    """尺寸线框图特征：底色黑白主导、线框像素占比小（<22%）、彩色占比极小（<6%）。
    官网尺寸图（细线+数字标注）都能命中；产品照片/促销横幅不会。"""
    im = _flat_white(im)
    im2 = im.resize((160, 160))
    total = 160 * 160
    ink = 0
    color = 0
    dark = 0
    light = 0
    for r, g, b in im2.getdata():
        mx, mn = max(r, g, b), min(r, g, b)
        lum = (r + g + b) // 3
        if mx - mn > 60:
            color += 1
        if 40 < lum < 220:
            ink += 1
        if lum <= 40:
            dark += 1
        if lum >= 220:
            light += 1
    bg = max(dark, light) / float(total)
    if bg < 0.55:
        return False
    # 大面积深色块 = 产品照（黑色机身/深色门体），尺寸线框图不会有大黑块
    if dark / float(total) > 0.15:
        return False
    if color / float(total) > 0.06:
        # 官网手绘风尺寸图常是大面积「单色」填充线框（BCD-620WICBU1 紫色实锤：彩色占 42%，
        # 32×32 缩图也不消失——不是细线是填充）。判别维度 = 色相集中度：
        # 彩色像素集中在单一 45° 扇区（≥85%）= 单色线框 → 放行；
        # 多彩色块（能效标红绿蓝箭头 / 海报照片）→ 否决。
        hues = []
        for r, g, b in im2.getdata():
            if max(r, g, b) - min(r, g, b) > 60:
                h, _, _ = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                hues.append(int(h * 360 // 45))
        mono = False
        if hues:
            top, ntop = Counter(hues).most_common(1)[0]
            mono = ntop / float(len(hues)) >= 0.85
        if not mono:
            return False
        # 单色大面积线框放行时跳过 ink 门槛：紫色填充亮度居中会被误计 ink（src_3 ink=44%）
    else:
        if ink / float(total) >= 0.22:
            return False
    # ---- 公共关（单色豁免通道和普通通道都要过，2026-10-22 橙色横幅从豁免通道混入实锤）----
    # ①边缘密度：线框图线条多（>3.5%），平滑的产品照/背板照只有 1% 上下
    g2 = im.convert("L").resize((200, 200))
    gp = list(g2.getdata())
    edge = 0
    for y in range(200):
        base = y * 200
        for x in range(200):
            if x + 1 < 200 and abs(gp[base + x] - gp[base + x + 1]) > 14:
                edge += 1
            if y + 1 < 200 and abs(gp[base + x] - gp[base + x + 200]) > 14:
                edge += 1
    if edge / float(total * 2) < 0.035:
        return False
    # ②灰阶过渡：线稿近二值（纯黑线+纯白底，mid60-180≤0.03），白底产品照满是柔和灰阶（≥0.5）
    #   （CXW-258 油烟机官网线稿实锤：黑色锅体横条不是照片）
    g3 = im.convert("L").resize((200, 200))
    d3 = list(g3.getdata())
    mid = sum(1 for v in d3 if 60 <= v < 180) / float(len(d3))
    if mid > 0.10:
        return False
    # ③最长暗程：照片实心块（近二值线稿豁免——官方线稿的黑色实条不是照片，91 长程合法）
    if mid > 0.05 and _max_dark_run(im) > 60:
        return False
    # ④内部空心：线稿空心、照片实心（536=0.277/216=0.310/EB65=0.534/220JE=0.838）
    return _interior_ink(im) <= 0.25


def _sat_frac(im):
    """高饱和像素占比（能效标识/海报的彩色箭头、边框会拉高；产品图以中性色为主）。"""
    im2 = im.convert("RGB").resize((160, 160))
    n = 0
    tot = 160 * 160
    for r, g, b in im2.getdata():
        if max(r, g, b) - min(r, g, b) > 60:
            n += 1
    return n / float(tot)


def _library_img(model, min_side=350):
    """软件图库优先（2026-10-21 用户实锤：官网抓链候选太差）：payload imgs = 卡片上显示的
    同款审过图；没有再看欣盛 ximg（缺图补的渲染图，yonyoucloud 直链无防盗链）。
    高饱和图（能效标识/海报）直接跳过——图库里有些型号存的就是标识图，不能要。
    min_side=0 时接受任意尺寸小图（最后兜底用）。"""
    import base64
    nm = norm_model(model) or model
    try:
        import payload as _pl
        p = _pl.get_payload()
        v = (p.get("imgs") or {}).get(nm)
        if isinstance(v, str) and v.startswith("data:image/"):
            im = Image.open(io.BytesIO(base64.b64decode(v.partition(",")[2])))
            im.load()
            if min(im.size) >= min_side and _sat_frac(im) < 0.08:
                return im.convert("RGB")
        for r in (p.get("rows") or []):
            if r.get("c") == nm and r.get("ximg"):
                try:
                    raw = fetch(r["ximg"], binary=True, timeout=20)
                    im = Image.open(io.BytesIO(raw))
                    im.load()
                    if min(im.size) >= min_side and _sat_frac(im) < 0.08:
                        return im.convert("RGB")
                except Exception:
                    pass
                break
    except Exception:
        pass
    return None


def _hunt_sizeimg_page(page_url, sizeimg, log, skip_urls=(), prod_path=None):
    """官网详情页定向找尺寸图（2026-10-21 用户规矩：有官方尺寸图就不用手绘）。
    URL/alt 带 尺寸/安装 字样的图优先，全部要过 _looks_like_sizeimg 线框检测才收。
    skip_urls：已选作产品主图的 URL 不参评（251WBIU1 白色门板照假阳性实锤）。"""
    try:
        html = fetch(page_url, extra_headers={
            "Referer": page_url.split("/")[0] + "//" + page_url.split("/")[2] + "/"}, timeout=20)
    except Exception:
        return False
    hints, generic = [], []
    # 规格参数区的官方规格图（class=js_specification_img 的 data-src）是官网尺寸图的固定位置，
    # 直接置顶——EB100Z35Pro2 实锤：它是 JPG，被下面「PNG 优先」排序压到 19 张之后，
    # generic 12 次预算永远轮不到，导致有官方尺寸图却走了手绘兜底（2026-10-28 用户实锤）。
    for sp in re.finditer(r'<img[^>]*js_specification_img[^>]*>', html, re.I):
        sm = (re.search(r'data-src="([^"]+?\.(?:png|jpe?g))"', sp.group(0), re.I)
              or re.search(r'src="([^"]+?\.(?:png|jpe?g))"', sp.group(0), re.I))
        if sm:
            u = sm.group(1)
            if u.startswith("//"):
                u = "https:" + u
            if u not in hints:
                hints.append(u)
    for m in re.finditer(r'(?:data-src|srcset|src)="([^"]+?\.(?:png|jpe?g))"', html, re.I):
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u   # casarte 懒加载页全是协议相对地址（CWY15 实锤）
        # alt 可能挂在同一 media 元素附近（source 标签没有 alt），就近窗口找
        am = re.search(r'alt="([^"]*)"', html[max(0, m.start() - 300):m.end() + 200], re.I)
        text = u + " " + (am.group(1) if am else "")
        host = urllib.parse.urlparse(u).netloc.lower()
        if not (host.endswith("haier.com") or host.endswith("casarte.com") or host.endswith("leader.com.cn")):
            continue
        # 页面骨架 UI 图直接排除（logo/占位符/横幅背景/进度条/图标）
        if re.search(r"logo|placeholder|banner_bg|progress_bar|icon|share|qrcode", u, re.I):
            continue
        if re.search(r"尺寸|安装|wgtj|canshu|chicun", text, re.I):
            hints.append(u)
        else:
            generic.append(u)
    # 线稿尺寸图几乎都是 PNG、产品照都是 JPG → PNG 优先少下无用图
    generic.sort(key=lambda u: 0 if u.lower().endswith(".png") else 1)
    ai_budget = [4]   # 详情页 AI 兜底认图预算（商汤免费额度，省着烧）
    tried = 0
    for u in dict.fromkeys(hints):
        if u in skip_urls:
            continue
        if _try_save_sizeimg(u, sizeimg, log, prod_path=prod_path, ai_budget=ai_budget):
            return True
    for u in dict.fromkeys(generic):
        if tried >= 12:
            break
        if u in skip_urls:
            continue
        tried += 1
        if _try_save_sizeimg(u, sizeimg, log, prod_path=prod_path, ai_budget=ai_budget):
            return True
    return False


def _try_save_sizeimg(u, sizeimg, log, prod_path=None, ai_budget=None):
    try:
        raw = fetch(u, extra_headers={
            "Referer": u.split("/")[0] + "//" + u.split("/")[2] + "/"}, binary=True, timeout=20)
        if len(raw) < 3000:
            return False
        im = Image.open(io.BytesIO(raw))
        if min(im.size) < 250:
            return False
        if _looks_like_sizeimg(im) or (
                ai_budget is not None and ai_budget[0] > 0 and _ai_is_sizeimg(im, log)):
            # AI 兜底认图有预算（默认 4 张）：线框启发式漏判的手绘风尺寸图靠它捞回
            if ai_budget is not None and not _looks_like_sizeimg(im):
                ai_budget[0] -= 1
            if prod_path and os.path.isfile(prod_path) and _same_img(im, Image.open(prod_path)):
                return False   # 与产品主图同图 → 是产品照不是尺寸图
            _flat_white(im).save(sizeimg, quality=92)
            if log:
                log("官网详情页命中官方尺寸图：%s" % u.split("/")[-1])
            return True
    except Exception:
        pass
    return False


def _rescue_prod(im, log):
    """本地超分救图（demo 人工 AI 流程的离线版）：短边不足 1080px 的机图
    LANCZOS 拉到 1080 + 温和锐化（2026-10-26 调参：原 radius=2/percent=80 在
    白底浅色机图上出光晕伪影——ES60H-Q3 桶身灰圈实锤——改 1.2/45 轻手笔）。
    1080 正好贴合蓝底卡画布，贴图不再二次放大。带 alpha 照常处理。"""
    w, h = im.size
    short = min(w, h)
    if short >= 1080:
        return im
    sc = 1080.0 / short
    im2 = im.resize((max(1, int(w * sc)), max(1, int(h * sc))), Image.LANCZOS)
    try:
        im2 = im2.filter(ImageFilter.UnsharpMask(radius=1.2, percent=45, threshold=3))
    except Exception:
        pass
    if log:
        log("机图只有 %dpx，已本地超分到 %dpx + 轻锐化" % (short, 1080))
    return im2


def _same_img(a, b, thr=0.6):
    """同图判定：墨迹二值化后 64×64 比对 IoU。
    必须先二值化再缩——细线尺寸图在灰度缩图里会消失，跟白底照片看起来一样（251WBIU1 实锤）。"""
    try:
        def ink(im):
            g = im.convert("L").resize((64, 64))
            return [1 if v < 215 else 0 for v in g.getdata()]
        ia, ib = ink(a), ink(b)
        union = sum(1 for x, y in zip(ia, ib) if x or y)
        if union < 40:
            return False   # 两张都近乎纯白，谈不上同图
        inter = sum(1 for x, y in zip(ia, ib) if x and y)
        return inter / float(union) >= thr
    except Exception:
        return False


def _pick_prod(model, img_urls, mdir, log, page_url=None, kind=""):
    """机图：①软件图库（payload imgs / 欣盛 ximg，与卡片同源）②抓取链候选做
    「边框同色+泛洪抠底」选抠出主体最大且落地的。官网尺寸线框图顺带识别存
    sizeimg.jpg 给 img4。横幅/场景图/拼图/悬浮创意图自动淘汰。"""
    os.makedirs(mdir, exist_ok=True)
    picked = os.path.join(mdir, "product.png")
    sizeimg = os.path.join(mdir, "sizeimg.jpg")
    have_size = os.path.isfile(sizeimg)
    if os.path.isfile(picked) and have_size:
        return picked
    # ① AI 选图先行（2026-10-29 用户令「蓝底也跟聚光一样会选好的图」）：
    # 复用聚光/发布图同款视觉选图（pei_core.zhipu_pick_images，缓存 _ai_pick_v4.json
    # 与聚光共享——同一台机器两边不重复烧视觉额度），取 front（正面整机）抠底。
    # 顺序教训（CWB12-27BKU1 实锤）：原来「软件图库」排在 AI 前面，图库里的开门
    # 内部照直接当选，AI 根本轮不到——聚光用官网关门整机照、蓝底却拿开门图。
    # 现在 AI 先挑（有缓存零成本），挑不中/AI 不可用/候选不足 → 退图库，再退启发式。
    have_prod = False
    if not have_prod:
        try:
            import pubwork as _pw
            _key = _pw._ai_key()
            if _key:
                import pei_core as _pc
                _src = os.path.join(_pw.sroot(), norm_model(model) or model)
                if os.path.isdir(_src):
                    _pick = _pc.zhipu_pick_images(_key, _src, kind or "", log=log)
                    if _pick and _pick.get("front"):
                        _fp = os.path.join(_src, _pick["front"])
                        if os.path.isfile(_fp):
                            _rescue_prod(Image.open(_fp), log).save(picked)
                            have_prod = True
                            if log:
                                log("机图用 AI 选图（front=%s，与聚光同源）" % _pick["front"])
        except Exception as _e:
            if log:
                log("AI 选图跳过：%r" % (_e,))
    # ② 软件图库兜底（与卡片同源的审过图；AI 没给出 front 时才用）
    if not have_prod:
        lib = _library_img(model)
        if lib:
            _rescue_prod(lib, log).save(picked)
            have_prod = True
            if log:
                log("机图用软件图库（与卡片同款）")
    # ② 候选源：写文案已生成的官方图（copy/<型号>/img_*.jpg）优先入队，再接抓取链 URL。
    # 没机图就选机图；有机图只找官网尺寸线框图
    srcs = []
    picked_url = ""
    try:
        nm2 = norm_model(model) or model
        cdir = os.path.join(copy_root(), nm2)
        srcs += [("file", p) for p in sorted(glob.glob(os.path.join(cdir, "img_*.jpg")))[:8]]
        # peiwork 爬虫缓存（官网页面下架后图仍在这，CXW-258 官网尺寸线稿实锤）
        # 2026-10-26 修「蓝底图又糊了」：素材根 10-24 改造后官网原图落在 peiwork/<型号>/官网/
        # 子目录（ES60H-Q3 1200px 大图在那躺着），旧 glob 只扫型号根目录→候选里全是小图，
        # 抓取链 600px 小图当选+超分发糊。补 官网/ 子目录并按字节降序，大图先占 24 席。
        try:
            import pubwork as _pw
            pdir = os.path.join(_pw.sroot(), nm2)
            _pc = (glob.glob(os.path.join(pdir, "*.png")) +
                   glob.glob(os.path.join(pdir, "*.jpg")) +
                   glob.glob(os.path.join(pdir, "官网", "*.png")) +
                   glob.glob(os.path.join(pdir, "官网", "*.jpg")))
            _pc.sort(key=lambda p: -os.path.getsize(p))
            srcs += [("file", p) for p in _pc[:24]]
        except Exception:
            pass
        if srcs and log:
            log("写文案/爬虫缓存 %d 张入候选（%s）" % (len(srcs), nm2))
    except Exception:
        pass
    srcs += [("url", u) for u in img_urls]
    best = None   # (score, PIL image)
    _trans_fallback = [None]   # 透明底小图暂存路径（等更大的变体）
    _ai_cand = []   # 尺寸图 AI 复核候选（本地路径，启发式漏判时商汤/GLM 兜底认）
    seen = set()
    n = 0
    for kind, u in srcs:
        # 2026-10-26 上限 24→40：官网/ 子目录按字节降序入队后，小字节的官方尺寸线稿
        # （22.jpg 13KB 实锤）被挤出局 → 尺寸图候选根本没进评比，img4 退化手绘
        if u in seen or n >= 40 or (have_prod and have_size):
            break
        seen.add(u); n += 1
        try:
            if kind == "url":
                raw = fetch(u, extra_headers={"Referer": u.split("/")[0] + "//" + u.split("/")[2] + "/"},
                            binary=True, timeout=20)
                if len(raw) < 3000:
                    continue
                fp = os.path.join(mdir, "src_%d.jpg" % n)
                with io.open(fp, "wb") as f:
                    f.write(raw)
                im = Image.open(fp)
            else:
                if not os.path.isfile(u):
                    continue
                im = Image.open(u)
            w, h = im.size
            if min(w, h) < 250:
                continue
            im.load()
            # 官网尺寸线框图判定放最前：官方线稿也是透明 PNG，不能被下面的
            # 透明底机图锁定抢走；判定/落盘全用垫白合成图（隐藏填充不暴露）
            if not have_size and _looks_like_sizeimg(im):
                # 已有机图时，候选若与机图同图（缩略图 vs 官网大图），它是产品照不是尺寸图
                if have_prod and os.path.isfile(picked) and _same_img(im, Image.open(picked)):
                    if log:
                        log("候选%d 与已选机图同图，是产品照不是尺寸图，跳过" % n)
                    continue
                _flat_white(im).save(sizeimg, quality=92)
                have_size = True
                if log:
                    log("候选%d 识别为官网尺寸图，img4 直接用它" % n)
                continue
            # 启发式没认出：先记进 AI 复核候选（本地路径，循环结束后商汤/GLM 看图定夺）
            if not have_size and len(_ai_cand) < 12:
                _ai_cand.append(fp if kind == "url" else u)
            # 官方透明底图（alpha 真透明）= 最佳机图。≥800px 直接锁；小图先记着，
            # 后面可能有同款 _1200 大变体（XQS100 600px 先到顶掉大图导致成图糊，实锤）
            rgba = im.convert("RGBA")
            alo, _ = rgba.split()[3].getextrema()
            if alo < 250 and not have_prod:
                bbox = rgba.split()[3].getbbox()
                if bbox and (bbox[2] - bbox[0]) >= 200 and (bbox[3] - bbox[1]) >= 300:
                    crop = rgba.crop(bbox)
                    if min(w, h) >= 800:
                        _rescue_prod(crop, log).save(picked)
                        have_prod = True
                        if kind == "url":
                            picked_url = u
                        if log:
                            log("候选%d 官方透明底图，直接锁定" % n)
                        continue
                    if not _trans_fallback[0]:
                        tp = os.path.join(mdir, "trans_fallback.png")
                        crop.save(tp)
                        _trans_fallback[0] = tp
                        if log:
                            log("候选%d 透明底图只有 %dpx，先记着等更大的" % (n, min(w, h)))
                    continue
            if have_prod:
                continue
            cut = _cut_border_bg(im)
            if cut is None:
                if log:
                    log("候选%d 背景不统一/形状不符，淘汰（%dx%d）" % (n, w, h))
                continue
            if cut[1] < 0.86:
                if log:
                    log("候选%d 主体悬浮不落地（底 %.0f%%），当创意图淘汰" % (n, cut[1] * 100))
                continue
            sf = _sat_frac(cut[0])
            if sf > 0.08:
                if log:
                    log("候选%d 彩色占比 %.0f%%——营销横幅/能效标识/场景图类，淘汰" % (n, sf * 100))
                continue
            score = cut[0].size[0] * cut[0].size[1]
            if log:
                log("候选%d 抠图成功 %dx%d 落地 %.0f%%" % (n, cut[0].size[0], cut[0].size[1], cut[1] * 100))
            if not best or score > best[0]:
                best = (score, cut[0], u if kind == "url" else "")
        except Exception as e:
            if log:
                log("候选%d 处理失败：%s" % (n, e))
    if not have_prod:
        if not best:
            if _trans_fallback[0]:
                _rescue_prod(Image.open(_trans_fallback[0]), log).save(picked)
                return picked
            if os.path.isfile(picked):
                return picked      # 预置机图（人工救回的成品），直接用
            return None
        _rescue_prod(best[1], log).save(picked)
        if best[2]:
            picked_url = best[2]
    if not have_size and _ai_cand:
        # 启发式全军覆没 → AI 视觉复核（2026-10-26 用户令：加了AI识别图就让它找图）。
        # 先按彩色占比升序：官方线稿近零彩色排前，产品照排后省预算
        def _color_ratio(p):
            try:
                im3 = _flat_white(Image.open(p)).convert("RGB").resize((80, 80))
                c = sum(1 for r, g, b in im3.getdata() if max(r, g, b) - min(r, g, b) > 60)
                return c / 6400.0
            except Exception:
                return 1.0
        for pth in sorted(_ai_cand[:20], key=_color_ratio)[:10]:
            try:
                im2 = Image.open(pth)
            except Exception:
                continue
            r = _ai_is_sizeimg(im2, log)
            if r:
                _flat_white(im2).save(sizeimg, quality=92)
                have_size = True
                if log:
                    log("AI 视觉认出官方尺寸图：%s" % os.path.basename(pth))
                break
            if r is None:
                if log:
                    log("视觉 AI 不可用，尺寸图复核跳过")
                break
    if not have_size and page_url:
        # 候选图里没认出尺寸图：去官网详情页定向翻一遍（有官方尺寸图就不用手绘）
        _hunt_sizeimg_page(page_url, sizeimg, log,
                           skip_urls=(picked_url,) if picked_url else (),
                           prod_path=picked if have_prod and os.path.isfile(picked) else None)
    return picked


# 官网/抓取链都拿不到尺寸时的已知库兜底（每条都要多方核实才准录入，键=型号原文+归一化）
# BCD-360WDCL: 深×宽×高 = 680×630×1905（ZOL详情/太平洋产品库/天马工业平台三源一致）
_KNOWN_DIMS = {
    "BCD-360WDCL": (630, 1905, 680),
}


def _parse_dims(pd):
    """从 props 解析外形尺寸 -> (宽, 高, 深)。
    ① 键里标了顺序（尺寸(宽*高*深)mm / 外形尺寸(深*宽*高)mm）→ 按标签对号入座；
    ② 没标签的「尺寸/外形尺寸」才按大小猜（最大=高、次大=宽、最小=深）。
    EB100Z35Pro2 实锤（2026-10-28）：官网同时给「尺寸」和「尺寸(宽*高*深)mm」同值键，
    先撞无标签键会把宽深对调（深 570 > 宽 550）——必须让带标签的键优先。"""
    items = list((pd or {}).items())
    def _nums(vs):
        ns = re.findall(r"\d{3,4}(?:\.\d+)?", str(vs))
        return [int(float(x)) for x in ns[-3:]] if len(ns) >= 3 else None
    for k, v in items:                  # ① 带顺序标签：对号入座
        ks = str(k)
        if ("尺寸" not in ks and "外形" not in ks):
            continue
        if not ("深" in ks and "宽" in ks and "高" in ks):
            continue
        nums = _nums(v)
        if not nums:
            continue
        order = []
        for ch, idx in (("深", 0), ("宽", 1), ("高", 2)):
            order.append((ks.find(ch), idx))
        order.sort()
        vals = [None, None, None]       # [宽, 高, 深]
        for pos, (_fpos, idx) in enumerate(order):
            nm = nums[pos]
            if idx == 0:
                vals[2] = nm            # 深
            elif idx == 1:
                vals[0] = nm            # 宽
            else:
                vals[1] = nm            # 高
        if all(vals):
            return tuple(vals)
    for k, v in items:                  # ② 无标签：按大小猜
        ks = str(k)
        if "尺寸" not in ks and "外形" not in ks:
            continue
        nums = _nums(v)
        if not nums:
            continue
        ns = sorted(nums, reverse=True)
        return ns[1], ns[0], ns[2]      # 兜底：宽=次大, 高=最大, 深=最小
    return None


def _ensure_dims(info, mdir, img_urls=None, log=None):
    """尺寸补漏（2026-10-29 用户令）：参数里没外形尺寸、又没有官方尺寸图时，
    去中关村参数页把「外形尺寸」等缺键并进 props（已有键绝不覆盖）。
    官网有图有参时主兜底不触发，但官网参数表可能就是没尺寸键（XQG100-H28E 实锤：
    ZOL 详情页 index2180682 有外形尺寸，官网没有）。蓝底 generate 与聚光 img_size 共用。
    返回 (info, img_urls)。有官方尺寸图时不补——图上数字才权威（ES60H-Q3 图文矛盾实锤）。"""
    log = log or (lambda s: None)
    model = (info.get("model") or "").strip()
    pd0 = pei_core.prop_dict(info.get("props") or [])
    have_si = bool(mdir and os.path.isfile(os.path.join(mdir, "sizeimg.jpg")))
    if have_si or _parse_dims(pd0) or _KNOWN_DIMS.get(model) \
            or _KNOWN_DIMS.get(norm_model(model) or ""):
        return info, img_urls
    log("参数里没外形尺寸，去中关村补…")
    try:
        zi, zp = _zol_data(model, (info.get("brand") or ""), log)
    except Exception as e:
        log("中关村补参失败：%r" % (e,))
        return info, img_urls
    if zi and not img_urls:
        img_urls = zi
    if zp:
        have = {str(k) for k, _v in (info.get("props") or [])}
        for k, v in zp:
            if str(k) not in have:
                info.setdefault("props", []).append((k, v))
        log("中关村补参：%d 条（合计 %d 条）" % (len(zp), len(info.get("props") or [])))
    return info, img_urls


def _kit_titles(brand, model, pd, info, sp):
    """蓝底套图标题三档（2026-10-22 三审定稿：品牌+卖点+新机+型号四要素）。
    卖点只从真实参数派生硬卖点（容量/门体/风冷/静音/变频/壁挂/大屏…），
    卖点句名里的服务话术（安装省心/日常好打理/官方正品）一概不要——2026-10-22
    用户两连实锤「一点卖点都没看出来」。"""
    feats = []
    caps = []
    noise_vals = []   # 多个噪音键取最小值只出一条（CGT90 64/69 两条重复实锤）
    def _add(x):
        if x and x not in feats and len(x) <= 7 and len(feats) < 5:
            feats.append(x)
    # 技术词：官网页标题里的招牌技术（XQS100-BZ6VE2「10公斤双动力波轮洗衣机」实锤）
    tt = str(info.get("title") or "")
    for w in ("双动力", "空气洗", "精华洗", "活水洗", "除螨", "防缠绕", "直驱"):
        if w in tt:
            _add(w)
    m_vol = re.search(r"(?<!\d)(\d{2,3})升", tt) if "热水器" in tt else None
    if m_vol:
        _add(m_vol.group(1) + "升大水量")
    m_hood = re.search(r"(顶吸|侧吸)", tt)
    if m_hood:
        _add(m_hood.group(1))
    for k, v in ((pd or {}).items()):
        ks, vs = str(k), str(v)
        if "直驱" in vs and ("电机" in ks or "变频" in ks or "电机" in vs):
            _add("直驱变频")
        if "制冷" in ks and "风冷" in vs:
            _add("风冷无霜")
        if "变频" in vs and ("压缩机" in ks or "电机" in ks or "变频" in ks):
            # 直驱变频与变频节能语义撞车，互斥只收一个（XQS100「直驱变频变频节能」实锤）
            if "直驱" in vs:
                _add("直驱变频")
            else:
                _add("变频节能")
        if "能效" in ks and "级" in vs:
            n = re.sub(r"[^1-3]", "", vs)
            # 只有一级能效算卖点，二三级不够格（2026-10-22 用户实锤"二级能效也能是卖点？"）
            if n == "1":
                _add("一级能效")
        if "屏幕尺寸" in ks or ("尺寸" in ks and "寸" in vs):
            n = re.sub(r"[^0-9]", "", vs)
            if n:
                _add(n + "寸大屏")
        if "噪音" in ks or "噪声" in ks:
            n = re.sub(r"[^0-9]", "", vs)
            if n:
                noise_vals.append(int(n))
        if "爆炒风量" in ks or "排风量" in ks:
            m = re.search(r"\d+", vs)
            # ≥18m³ 才算大风量卖点（工作风量 11 不够格）；search 首段数字
            # （[^0-9] 会把 21m³/min 剥成 213，³ 被拼错位实锤）
            if m and 18 <= int(m.group(0)) <= 40:
                _add(m.group(0) + "m³大风量")
        if "热水产率" in ks:
            m = re.search(r"\d+", vs)
            if m:
                _add(m.group(0) + "升大水量")
        if ("造型款式" in ks or ks == "款式"):
            for w in ("顶吸", "侧吸"):
                if w in vs:
                    _add(w)
                    break
        if "容积" in ks or ("容量" in ks and "kg" not in vs.lower()):
            n = re.sub(r"[^0-9]", "", vs)
            if n:
                # 洗碗机容量单位 件/套（CWY15 总容积=15件 实锤），冰箱冷柜才是 L
                caps.append(("总" in ks, int(n), "套" if ("套" in vs or "件" in vs) else "L"))
        if "洗涤容量" in ks and ("套" in vs or "件" in vs):
            n = re.sub(r"[^0-9]", "", vs)
            if n:
                caps.append((True, int(n), "套"))
        if "洗涤容量" in ks and "kg" in vs.lower():
            # 洗衣机公斤容量也是大容量卖点（XQS100「10公斤大容量」漏收实锤）
            n = re.sub(r"[^0-9.]", "", vs)
            if n:
                caps.append((True, int(float(n)), "公斤"))
        if "干燥方式" in ks and vs and vs not in ("无", "否", "不支持"):
            _add(str(vs)[:7])  # 智能开门速干
        if "消毒方式" in ks and vs and vs not in ("无", "否", "不支持"):
            _add(str(vs)[:2] + "消毒")  # 高温消毒
        if "洗烘" in ks + vs:
            _add("洗烘一体")
        if ("温区" in ks or "温控" in ks) and ("三" in vs or "变温" in vs):
            _add("多温区")
        if "壁挂" in ks and vs in ("是", "支持", "有", "具备"):
            _add("支持壁挂")
        if ("工作原理" in ks or "滤芯" in ks or "渗透" in ks or "过滤" in ks) and (
                "RO" in vs.upper() or "反渗透" in vs):
            _add("RO反渗透")  # 净水机（HKC3000 实锤：参数有 RO 膜但标题空）
        if "通量" in ks and vs:
            n = re.sub(r"[^0-9G]", "", vs.upper())
            if n:
                _add(n + "大通量")
        if ("箱门" in ks or "门体" in ks or "开门" in ks or "结构" in ks):
            for w, t in (("十字", "十字门"), ("对开", "对开门"), ("多门", "多门"),
                         ("三门", "三门"), ("双门", "双门")):
                if w in vs:
                    _add(t)
                    break
    if caps:
        best = max(caps, key=lambda t: (t[0], t[1]))  # 优先总容积，否则最大数
        _add("%d%s大容量" % (best[1], best[2]))
    if noise_vals:
        _add("%d分贝静音" % min(noise_vals))
    # 卖点短语化：按分量排序后贪心拼接（每条最多 2 个卖点），同组合去重，单卖点补位
    def _rank(x):
        if "大容量" in x or "大屏" in x or "大水量" in x or "大风量" in x:
            return 0
        if "门" in x and x.endswith("门"):
            return 1
        if "风冷" in x or "洗烘" in x:
            return 2
        if "静音" in x:
            return 3
        if x.startswith("变频"):
            return 4
        if "温区" in x or "壁挂" in x:
            return 5
        return 6
    # dict.fromkeys 保序去重——set 无序（中文 hash 每次进程都变），
    # 主卖点顺序会随机漂移（title 招牌词「双动力」可能被挤到后面，实锤）
    order = sorted(dict.fromkeys(feats), key=_rank)
    budget = max(4, 30 - len(brand) - 2 - len(model) - 3)  # 3 个分隔空格也占字数
    # 三条标题三路走（2026-10-23 用户拍板样板）：技术 / 容量+能效 / 能效+技术，
    # 条条主卖点不同，不再互相排列组合（XQS100 三条全是变频+能效的子集实锤）
    def _bucket(x):
        if "大容量" in x or "大屏" in x or "大水量" in x or "大风量" in x or x.endswith("门"):
            return "cap"
        if "能效" in x or "静音" in x or x.startswith("变频"):
            return "eff"
        return "tech"
    buckets = {"tech": [], "cap": [], "eff": []}
    for f in order:
        buckets[_bucket(f)].append(f)
    phrases = []
    seen_sets = []

    def _try_add(cur):
        if cur and frozenset(g for g in order if g in cur) not in seen_sets:
            seen_sets.append(frozenset(g for g in order if g in cur))
            phrases.append(cur)
            return True
        return False

    used_mains = set()
    for main, sub in (("tech", "cap"), ("cap", "eff"), ("tech", "eff"),
                      ("eff", "tech"), ("cap", "tech"), ("eff", "cap")):
        if len(phrases) >= 3:
            break
        mains = buckets[main] or buckets[sub] or order
        subs = buckets[sub] or buckets[main] or order
        # 主卖点用没用过的（三条路各一个主心骨），主桶空了才复用
        main_f = next((f for f in mains if f not in used_mains), mains[0] if mains else "")
        cur = main_f if main_f and len(main_f) <= budget else ""
        for f in (subs + order):
            if f in cur:
                continue
            if len(cur) + len(f) <= budget:
                cur += f
            if sum(1 for g in order if g in cur) >= 2:
                break  # 每条最多 2 个卖点，多了变复读（55A30 实锤）
        if _try_add(cur) and main_f:
            used_mains.add(main_f)
    # 三路没凑满三条：按 rank 顺序单卖点补位
    for f in order:
        if len(phrases) >= 3:
            break
        if frozenset([f]) not in seen_sets:
            seen_sets.append(frozenset([f]))
            phrases.append(f)
    if not phrases:
        # 参数完全派生不出卖点（冷门品类）：退回卖点句名，三条全同比空白强
        for s_ in (sp or [])[:8]:
            nm = re.split(r"[，,]", str(s_))[0].strip()
            nm = re.sub(r"(动力|功能|设计|升级)$", "", nm)
            if 3 <= len(nm) <= 7 and not re.search(r"全新|清仓|特价|钣金|低价|用料扎实|安装|打理|正品|验货|开箱", nm):
                if nm not in phrases:
                    phrases.append(nm)
            if len(phrases) >= 3:
                break
    if len(phrases) < 3:
        # 卖点凑不满三条：品类词/裸标题补位，绝不三条全同（2026-10-22 CWY15-29BKU1 实锤）
        _cat = ""
        try:
            import pei_core as _pc
            _cat = _pc.cat_from_url(info.get("url") or "") or (_pc.cat_name(info) or "").strip()
        except Exception:
            _cat = ""
        for fill in ((_cat or "") + "新机", "新机"):
            ft = fill.replace("新机", "").strip()
            key = ft if ft else "BARE"
            if key not in seen_sets and brand + " " + fill + " " + model != brand + " " + (phrases[0] if phrases else "") + " 新机 " + model:
                seen_sets.append(key)
                phrases.append(ft)
            if len(phrases) >= 3:
                break
    # 四要素：品牌+卖点+新机+型号，空格分隔（2026-10-22 用户令：标题要有间隔）；空段不留双空格
    out = []
    for p in (phrases[:3] if phrases else [""] * 3):
        out.append(" ".join(x for x in (brand, p.strip(), "新机", model) if x.strip()))
    return out


def _hard_points(info, pd, sp):
    """卖点一话一行（2026-10-23 用户拍板样板口径）：每行具体硬货
    （双动力双电机反向旋洗/床单被罩一次洗完/电费不到一度），
    不要安装省心、日常好打理这类虚话。品类话术模板优先，官网卖点硬行兜底。"""
    cat = str(info.get("cat") or "")
    tt = str(info.get("title") or "")
    alltext = tt + " " + " ".join(str(k) + str(v) for k, v in (info.get("props") or []))
    out = []

    def num(v):
        m = re.search(r"\d+(\.\d+)?", str(v))
        return m.group(0) if m else None

    cap_kg = cap_l = cap_set = None
    eff1 = bool(re.search(r"1级|一级", str(pd.get("能效等级", ""))))
    inv = "变频" in alltext
    wide = None
    for k, v in pd.items():
        ks, vs = str(k), str(v)
        if cap_kg is None and "洗涤容量" in ks and "kg" in vs.lower():
            cap_kg = num(vs)
        if cap_set is None and ("洗涤容量" in ks or "套数" in ks) and ("套" in vs or "件" in vs):
            cap_set = num(vs)
        if cap_l is None and ("总容积" in ks or ("容积" in ks and "L" in vs.upper())):
            cap_l = num(vs)
        if wide is None and "尺寸" in ks:
            m = re.search(r"(\d{3})", vs)
            if m:
                wide = int(m.group(1))

    def add(s):
        if not s or len(out) >= 6:
            return
        # 按卖点名（逗号前段）去重：模板行和官网兜底行常同名不同尾，
        # 整句比对拦不住，图上就会出现两张「80升大水量」（10-26 实锤）
        nm = re.split(r"[，,]", str(s))[0].strip()
        if any(re.split(r"[，,]", x)[0].strip() == nm for x in out):
            return
        out.append(s)

    def _eff_line(pre=""):
        if eff1:
            add((pre + "一级能效变频电机，省电安静噪音低") if inv else (pre + "一级能效，洗一次电费不到一度"))
        elif inv:
            add(pre + "变频电机，省电安静噪音低")

    if "洗衣机" in cat or "波轮" in tt or "滚筒" in tt:
        if "双动力" in alltext:
            add("双动力双电机，内外桶反向旋洗，洗净比高还防缠绕")
        if "直驱" in alltext:
            add("直驱变频电机，动力强噪音小，衣服磨损更小")
        if "洗烘" in alltext:
            add("洗烘一体，洗完直接穿，阴雨天不用愁")
        if cap_kg:
            add("%s公斤大容量，床单被罩四件套一次洗完" % cap_kg)
        _eff_line()
        if wide and wide <= 600:
            add("%dmm超薄机身，小阳台小空间也放得下" % wide)
    elif "冰箱" in cat or "冷柜" in tt or "冰柜" in tt:
        if "风冷" in alltext:
            add("风冷无霜，不用除霜不冻手")
        if cap_l:
            add("%s升大容量，一周食材一次装下" % cap_l)
        if "十字" in alltext:
            add("十字对开门，生熟分储不串味")
        elif "多门" in alltext:
            add("多门分储，食材各归各位")
        elif "对开" in alltext:
            add("对开门大格局，食材一目了然")
        if "净味" in alltext or "除菌" in alltext or "杀菌" in alltext:
            add("净味除菌，食材更保鲜不串味")
        if eff1:
            add("一级能效变频压缩机，恒温保鲜更安静" if inv else "一级能效，一天电费不到一度")
        elif inv:
            add("变频压缩机，恒温保鲜更安静")
    elif "洗碗机" in cat or "洗碗" in tt:
        if cap_set:
            add("%s套大容量，锅碗瓢盆一次洗完" % cap_set)
        if "开门速干" in alltext or "干燥方式" in str(pd):
            add("智能开门速干，洗完即干不闷味")
        if "消毒" in alltext or "除菌" in alltext or "杀菌" in alltext:
            add("高温除菌消毒，比手洗洗得更干净")
        if "嵌" in alltext:
            add("嵌入式安装，橱柜里一点不占地")
    elif "电视" in cat or "电视" in tt:
        n = num(str(pd.get("屏幕尺寸", "")) or (re.search(r"(\d{2,3})寸", tt).group(1) if re.search(r"(\d{2,3})寸", tt) else None))
        if n:
            add("%s寸大屏，客厅观影够沉浸" % n)
        if "4K" in alltext.upper():
            add("4K超高清，画面细节清清楚楚")
        _eff_line()
        if "语音" in alltext:
            add("语音遥控，动嘴就能换台")
    elif "空调" in cat or "空调" in tt:
        n = num(str(pd.get("匹数", "")))
        if n:
            add("%s匹，客厅卧室都带得动" % n)
        if "自清洁" in alltext:
            add("自清洁内机，吹出来的风更干净")
        _eff_line()
    elif "净水" in cat or "净水" in tt:
        if "RO" in alltext.upper() or "反渗透" in alltext:
            add("RO反渗透，滤完直接喝")
        n = num(str(pd.get("通量", "")))
        if n:
            add("%sG大通量，接水不用等" % n)
    elif "油烟机" in tt or "烟机" in tt or "爆炒风量" in str(pd) or "排风量" in str(pd):
        n = None
        for k, v in pd.items():
            ks = str(k)
            if "爆炒风量" in ks or "排风量" in ks:
                m = re.search(r"(\d+)", str(v))
                if m and int(m.group(1)) >= 18:
                    n = m.group(1)
                    break
        if n:
            add("%sm³大风量，爆炒油烟不过脸" % n)
        n2 = num(str(pd.get("半消声室噪声(dB(A))", "")) or str(pd.get("半消声室噪音", "")))
        if n2:
            add("%s分贝低噪，静音不扰眠" % n2)
        if eff1:
            add("一级能效，天天做饭电费不心疼")
        nums_dim = re.findall(r"(\d{3,4})", str(pd.get("外形尺寸(深*宽*高)mm", "")) or str(pd.get("外形尺寸", "")))
        if nums_dim and int(nums_dim[0]) <= 400:
            add("%smm超薄机身，不碰头更省空间" % nums_dim[0])
        elif len(nums_dim) >= 2 and int(nums_dim[1]) >= 800:
            add("%smm宽拢烟腔，大锅爆炒也罩得住" % nums_dim[1])
    elif "燃气" in tt or "热水产率" in str(pd) or "气源" in str(pd):
        n = num(str(pd.get("热水产率(L/min)", "")))
        if not n:
            m = re.search(r"(\d{2})升", tt)
            n = m.group(1) if m else None
        if n:
            add("%s升大水量，全家接力洗不等热水" % n)
        if "CO" in alltext.upper() and "防护" in alltext:
            add("CO安全防护，用得暖更用得安心")
        elif "熄火" in alltext or "防干烧" in alltext or "双气" in alltext:
            add("多重安全防护，熄火断气自动保护")
        if "宽频" in alltext and "水温" in alltext:
            add("宽频调温，夏天不烫冬天够暖")
        if "抑菌" in tt:
            add("高温抑菌，洗澡水更干净")
    elif "热水器" in cat or "热水器" in tt:
        n = num(str(pd.get("容积", "")))
        if not n:
            m = re.search(r"(\d{2})升", tt)
            n = m.group(1) if m else None
        if n:
            add("%s升大水量，一家三口洗澡够用" % n)
        if "三层胆" in alltext or "金刚" in alltext:
            add("金刚三层胆，内胆结实不漏水")
        if "速热" in alltext:
            add("储热速热二合一，随用随热不用等")
        if "保温" in alltext:
            add("360°全方位保温，热水放到晚上还是烫的")
        elif "抑菌" in tt:
            add("高温抑菌内胆，洗澡水更干净")
        if "防干烧" in alltext or "超温" in alltext:
            add("多重安全防护，防干烧防超温")
    # 模板没凑满：官网卖点里挑硬行补（含数字或带解释的），虚话一概不要
    if len(out) < 6:
        for s_ in (sp or []):
            s_ = str(s_).strip()
            if not s_ or re.search(r"安装省心|师傅上门|日常好打理|省时省力|用料扎实|细节经得起|官方正品|验货|开箱|[23]级能效", s_):
                continue
            if re.search(r"\d", s_) or "，" in s_:
                add(s_)
            if len(out) >= 6:
                break
    return out


def _short_feats(pd, info):
    """img1 底部卖点行：3-4 个真实参数短词。"""
    out = []
    def add(s):
        if s and s not in out and len(out) < 4:
            out.append(s)
    for k, v in pd.items():
        ks, vs = str(k), str(v)
        if "制冷方式" in ks and "风冷" in vs:
            add("风冷无霜")
        if "制冷方式" in ks and "风直冷" in vs:
            add("风直冷")
        if ("温区" in ks or "温控" in ks) and ("三" in vs or "多" in vs or "变温" in vs):
            add("三温区")
        if ("物联" in ks or "智能" in ks or "WiFi" in ks.upper()) and (
                vs in ("支持", "是", "有", "具备") or "WiFi" in vs.upper() or "远程" in vs):
            add("智能WiFi")
        if ("箱门" in ks or "门体" in ks or "门材质" in ks) and "玻璃" in vs:
            add("玻璃门")
        if ("能效" in ks) and vs and re.sub(r"[^1-3]", "", vs) == "1":  # 只有一级能效算卖点
            add(vs.replace("1", "一").replace("2", "二").replace("3", "三") + ("级能效" if "级" not in vs else ""))
        if "噪音" in ks or "噪声" in ks:
            n = re.sub(r"[^0-9.]", "", vs)
            if n:
                add(n + "分贝静音")
        if "电机" in ks and "变频" in vs:
            add("变频")
        if "变频" in ks and vs in ("变频", "是", "支持"):
            add("变频")
        if "面板" in ks and vs and vs not in ("无", "否") and vs not in (
                "钣金", "金属", "塑料", "玻璃", "PCM", "彩涂板", "不锈钢"):  # 材质词不是卖点（2026-10-22 钣金实锤）
            add(vs)
        if "烘干" in ks and vs not in ("不支持", "无", "否"):
            add("洗烘一体")
        # 洗碗机/容量类（CWY15 实锤：底部只剩「大容量」一个词）；容量单位 件/套
        if ("套" in vs or "件" in vs) and ("容量" in ks or "套数" in ks or "容积" in ks):
            n = re.sub(r"[^0-9]", "", vs)
            if n:
                add(n + "套大容量")
        elif "容积" in ks or ("容量" in ks and "kg" not in vs.lower()):
            n = re.sub(r"[^0-9]", "", vs)
            if n:
                add(n + "L大容量")
        if ("除菌" in ks or "消毒" in ks or "杀菌" in ks) and vs not in ("无", "否", "不支持"):
            add("除菌消毒")
        if "干燥方式" in ks and vs and vs not in ("无", "否", "不支持"):
            add(str(vs).replace("式", "")[:6])  # 智能开门速干
        if ("安装" in ks or "产品类型" in ks) and "嵌" in vs:
            add("嵌入式")
        if ("洗涤方式" in ks or "烘干方式" in ks or "干燥方式" in ks) and vs and vs not in ("无", "否", "不支持"):
            add(str(vs).replace("式", "")[:6])
    return " · ".join(out) if out else (pei_core.core_feature(info) or "")


def _payload_row(model):
    """payload rows 里找本型号的行（用户自己的价格表数据）：b=品牌 n=货源名称 mo/c=型号。"""
    try:
        import payload as _pl
        nm = norm_model(model) or model
        for r in (_pl.get_payload().get("rows") or []):
            if norm_model(str(r.get("mo") or r.get("c") or "")) == nm:
                return r
    except Exception:
        pass
    return None


def _split_name(name, model):
    """货源名称 → (line1, line2)：按语义断点切，型号永远在 line2 尾部。
    2026-10-24 用户令：胶囊拆分必须读得顺，禁止 len//2 腰斩（「一级直|冷三门」实锤）——
    优先在品类词后切（海尔冰箱 | 一级直冷…），没有品类词在门体/制冷方式词前切，
    最后才是数字规格前。渲染层再把型号拆成单独一颗（_render_img1）。"""
    name = re.sub(r"\s+", "", name or "")
    if not name or not model or not name.endswith(model) or len(name) <= len(model):
        return None
    body = name[:-len(model)]
    cut = None
    for kw in ("冰箱", "冷柜", "冰柜", "洗衣机", "干衣机", "空调", "热水器", "净水器",
               "油烟机", "灶具", "洗碗机", "微波炉", "烤箱", "电视", "酒柜", "消毒柜",
               "投影仪", "电风扇", "饮水机", "取暖器", "电暖器"):
        i = body.find(kw)
        if i >= 0:
            # 品类词后面还有字 → 在品类词后切；品类词正好收尾 → 在品类词前切
            # （统帅智能投影仪LTR1 → 统帅智能 | 投影仪LTR1，原例口径）
            cut = i + len(kw) if i + len(kw) < len(body) else i
            break
    if cut is None:
        m = re.search(r"(单门|双门|两门|三门|四门|多门|十字门|对开门|直冷|风冷|变频|定频)", body)
        if m and m.start() > 0:
            cut = m.start()
    if cut is None:
        m = re.search(r"\d", body)
        if m and m.start() >= 2:
            cut = m.start()
    if cut is None or cut < 2:
        return None
    line1, line2 = body[:cut], body[cut:]
    # 欣盛名把自家系列码直接拼在型号前（15套亚式29B+CWY15-29BKU1，实锤），
    # 尾部一段 ≤4 位纯字母数字码且前面是中文的，看着像型号一部分，剥掉
    m2 = re.search(r"([A-Za-z0-9]{1,4})$", line2)
    if m2 and line2[:m2.start()] and re.search(r"[\u4e00-\u9fff]$", line2[:m2.start()]):
        line2 = line2[:m2.start()]
    return line1, line2 + model


def _build_cfg(info, prod_size_ok=True, mdir=None, ai_dims=None):
    model = (info.get("model") or "").strip()
    # 用户令（2026-10-21）：首图胶囊必须取自软件「货源名称」字段，不许自由发挥
    row = _payload_row(model)
    if row and row.get("b"):
        info["brand"] = str(row["b"]).strip()
    brand = (info.get("brand") or "海尔").strip()
    cat = pei_core.cat_name(info) or ""
    cap = pei_core.capacity_str(info) or ""
    feat = (pei_core.core_feature(info) or "") if (info.get("props") or []) else ""
    tw = pei_core.type_word(info) or ""
    brand_en = {"海尔": "HAIER", "卡萨帝": "CASARTE", "统帅": "LEADER", "Leader": "LEADER"}.get(brand, brand.upper())
    line1 = (brand + cat + cap) or (brand + cat)
    line2 = ((feat + tw) if feat else tw) + model
    if not line2.strip(model):
        line2 = model
    pd = pei_core.prop_dict(info.get("props") or [])
    # 胶囊 = 货源名称原话拆两段（用户令：不许自由发挥）
    if row:
        split = _split_name(str(row.get("n") or ""), model)
        if split:
            line1, line2 = split
    rows = []
    for k, v in (info.get("props") or []):
        ks, vs = str(k), str(v)
        if not vs or any(x in ks for x in _SKIP_KEYS):
            continue
        rows.append((ks, vs))
        if len(rows) >= 8:
            break
    # 尺寸：参数没有时查多方核实过的已知库（360WDCL=宽630×高1905×深680，ZOL/太平洋/天马三源一致）；
    # 都没有就不标数字——瞎标 (500,800,500) 是造假（2026-10-22 自查砍掉）
    dims = _parse_dims(pd) or _KNOWN_DIMS.get(model) or _KNOWN_DIMS.get(norm_model(model) or "")
    # 官方尺寸图在场时的口径（2026-10-26）：AI 从图上读出的数字最权威；
    # AI 没读出就 dims=None 不印数字（参数表数字跟官方图打架 = 图文矛盾，ES60H-Q3 实锤）
    _si_chk = os.path.join(mdir, "sizeimg.jpg") if mdir else None
    _have_si = bool(_si_chk and os.path.isfile(_si_chk))
    if ai_dims:
        dims = list(ai_dims)
    elif _have_si:
        dims = None
    if cap and cap[0].isdigit():
        capn = re.sub(r"[^0-9.]", "", cap.split("kg")[0] if "kg" in cap else cap)
    else:
        capn = ""
    size_pills = ["%s × %s mm" % (dims[0], dims[1]), "深 %s mm" % dims[2]] if dims else []
    sp = [str(x).strip() for x in pei_core.sellpoints(info) if str(x).strip()]
    if not sp:
        sp = [str(x).strip() for x in (info.get("selling") or []) if str(x).strip()]
    # 卖点硬货化（2026-10-23 用户拍板样板口径）：img3 格子与文案区共用同一来源
    hard = _hard_points(info, pd, sp)
    cells = []
    for i, s in enumerate(hard[:6]):
        s = str(s).strip().replace("dB(A)", "分贝").replace("dB", "分贝")
        if "，" in s:
            nm, dsc = s.split("，", 1)
        elif "," in s:
            nm, dsc = s.split(",", 1)
        else:
            nm, dsc = s, s
        cells.append(("%02d" % (i + 1), nm[:12], dsc))
    fpills = []
    for k, v in (info.get("props") or []):
        vs, ks = str(v).strip(), str(k)
        if (vs in ("支持", "是", "有", "具备", "yes", "Yes") and ks not in fpills
                and len(ks) <= 10 and "尺寸" not in ks and "颜色" not in ks):
            # 键名是问题式（是否可壁挂/是否配备底座）→ 剥掉「是否」变成肯定短词
            pill = re.sub(r"^是否", "", ks).strip()
            if pill and pill not in fpills:
                fpills.append(pill)
        if len(fpills) >= 8:
            break
    if len(fpills) < 6:
        # 「支持」型参数不满 6 颗：卖点短句名（"风冷无霜，免手动除霜"→风冷无霜）+ 参数短词补位，
        # 绝不让「同时支持」空着或只挂一两颗（2026-10-21/22 用户两连实锤）
        for s_ in (sp or [])[:8]:
            nm = re.split(r"[，,]", str(s_))[0].strip()
            nm = re.sub(r"(动力|功能|设计)$", "", nm)
            if 2 <= len(nm) <= 7 and nm not in fpills:
                fpills.append(nm)
            if len(fpills) >= 6:
                break
        for x in _short_feats(pd, info).split(" · "):
            x = x.strip()
            if x and x not in fpills and len(fpills) < 6:
                fpills.append(x)
    titles = _kit_titles(brand, model, pd, info, sp)
    # 文案区卖点 = 硬货模板行（2026-10-23 用户拍板），不再官网句照抄；
    # 2026-10-26 卖点图扩卡：上限 4→6（写文案是独立管线不读这里，不受影响）
    points = [s.replace("dB(A)", "分贝").replace("dB", "分贝") for s in hard]
    # 2026-10-26 实锤坑：官网原始型号常带后缀（ES60H-Q3白），norm_model 不剥「白」，
    # model_dir(model) 会指到 kitset/ES60H-Q3白/ 而 generate 实际用归一化目录
    # kitset/ES60H-Q3/ —— sizeimg.jpg 明明落了盘这里却 isfile=False，img4 永远退化手绘。
    # 改为 generate 直传 mdir（同一次调用里落盘和读取必须同一目录）。
    _si = os.path.join(mdir, "sizeimg.jpg") if mdir else os.path.join(model_dir(model), "sizeimg.jpg")
    cfg = {
        "brand": brand, "brand_en": brand_en, "model": model, "cat": cat, "cap": cap,
        "series": (brand + cat) or brand,
        "line1": line1, "line2": line2,
        "features_line": _short_feats(pd, info),
        "params_rows": rows,
        "size_label": "尺寸（宽×高×深）",
        "size_pills": size_pills,
        "dims": list(dims) if dims else None,
        "cells": cells,
        "feat_label": "同时支持",
        "feature_pills": fpills,
        "sizeimg": _si if os.path.isfile(_si) else None,
        "url": info.get("url") or "",
    }
    # 售后三句定稿（2026-10-23 用户拍板版本 B：要带一句互动引导）
    aftersale = ("全新未拆封，%s全国联保。\n支持到货验货，破损可拒收。\n"
                 "看中了就联系我，聊得来价格好商量。" % brand)
    return cfg, titles, points, aftersale


def generate(model, force=0, log=None):
    log = log or (lambda s: None)
    if not os.path.isfile(F_REG):
        raise RuntimeError("缺系统字体 msyh.ttc（%s）" % F_REG)
    mdir = model_dir(model)
    if force:
        # 强制重出时清掉旧的尺寸图缓存（251WBIU1 白门板假阳性实锤：不改代码也得能自愈）
        _stale = os.path.join(mdir, "sizeimg.jpg")
        if os.path.isfile(_stale):
            try:
                os.remove(_stale)
            except Exception:
                pass
    cache = os.path.join(mdir, "set.json")
    if not force and os.path.isfile(cache):
        try:
            with io.open(cache, encoding="utf-8") as f:
                res = json.load(f)
            res["cached"] = True
            return res
        except Exception:
            pass
    log("抓参数/机图（爬虫→官网→欣盛）…")
    info, img_urls = _gather_info(model, log)
    zimgs, zprops = ([], [])
    if not img_urls or not (info.get("props") or []):
        # 官网/欣盛没图或没参数：中关村在线兜底（用户令 2026-10-21）
        log("常规渠道缺料，试中关村…")
        zimgs, zprops = _zol_data(model, (info or {}).get("brand") or "", log)
        if zimgs and not img_urls:
            img_urls = zimgs
        if zprops and not (info.get("props") or []):
            info["props"] = zprops
    # 尺寸补漏（2026-10-29 用户令「一个机器怎么可能没有尺寸」+「中关村的也可以找找数据」）：
    # 官网有图有参时上面那道兜底不触发，但官网参数表可能就是没有外形尺寸键
    # （XQG100-H28E 实锤）——中关村参数页补一次（详见 _ensure_dims）。
    info, img_urls = _ensure_dims(info, mdir, img_urls, log)
    prod = _pick_prod(model, img_urls, mdir, log, page_url=(info or {}).get("url") or None,
                      kind=(info or {}).get("kind") or (info or {}).get("cat") or "")
    if not prod and zimgs:
        log("中关村图单独再试一轮…")
        prod = _pick_prod(model, zimgs, mdir, log)
    if not prod:
        # 最后兜底：卡片缩略图（小图，清晰度有限但好过失败）
        lib2 = _library_img(model, min_side=0)
        if lib2:
            _rescue_prod(lib2, log).save(os.path.join(mdir, "product.png"))
            prod = os.path.join(mdir, "product.png")
            if log:
                log("用卡片缩略图凑（清晰度有限，建议在写文案里补张高清图）")
    if not prod:
        raise RuntimeError("没抓到可用机图——先在「写文案」里生成一次图片，或确认欣盛/官网/中关村有该型号图")
    log("组装套图数据…")
    _ai_dims = _ai_read_size_nums(os.path.join(mdir, "sizeimg.jpg"), log) if (
        mdir and os.path.isfile(os.path.join(mdir, "sizeimg.jpg"))) else None
    cfg, titles, points, aftersale = _build_cfg(info, mdir=mdir, ai_dims=_ai_dims)
    log("渲染四张 1080×1080…")
    _render_img1(cfg, Image.open(prod), os.path.join(mdir, "img_1.jpg"))
    _render_img2(cfg, Image.open(prod), os.path.join(mdir, "img_2.jpg"))
    _render_img3(cfg, points, os.path.join(mdir, "img_3.jpg"))
    _render_img4(cfg, os.path.join(mdir, "img_4.jpg"), prod)
    res = {"ok": True, "cached": False, "model": cfg["model"], "name": cfg["series"] + " " + cfg["model"],
           "titles": titles, "points": points, "aftersale": aftersale,
           "params": dict(cfg["params_rows"]), "src": cfg["url"],
           "imgs": ["1", "2", "3", "4"]}
    with io.open(cache, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    return res


