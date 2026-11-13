# -*- coding: utf-8 -*-
"""wm_paper.py — 纸感白底 + 双层隐藏水印（2026-11-13 用户定稿）。

两层水印（都藏在底色里，不出文字区干扰可读性——低浓度叠在文字上亦无感）：
① 融字水印：文案斜向平铺，约 5% 不透明度（ALPHA=13/255），放大 2.8 倍可读；
② 微点阵：密钥派生伪随机点序，人眼无感（每点只比底色深约 4 灰阶），
   怀疑盗图时用 dot_positions() 重建点序比对即可验证。

种子固定 → 每台机器纹理/水印一致、可复现；纯程序绘制，零耗时零额度。

对外入口：
- paper_bg(w, h)                 纸感白 B3 底（#FEFEFC，聚光用）
- stamp_save(im, out, quality)   按输出路径自动盖章：路径含「蓝底」目录用「东星斑」、
                                 含「聚光」目录用「科科」；其他目录原样保存不盖章。

字体跨平台：Win 用微软雅黑，Mac 依次试苹方/黑体，都不在退 load_default。
"""
import hashlib
import math
import os
import random

from PIL import Image, ImageDraw, ImageFilter, ImageFont

WM_ON = True                       # 总开关（出问题可一键关）
ALPHA = 13                         # 融字水印不透明度（13/255 ≈ 5%）
WM_COL = (178, 170, 154)           # 暖灰水印色
B3 = (254, 254, 252)               # 纸感白底色（用户定稿档）
SECRET = "TINGBAO-PAPER-2026"      # 微点阵密钥（勿入仓库外任何明文）
TXT = {
    "spot": "当代打太极的科科",     # 聚光水印文案（用户 2026-11-13 定稿）
    "blue": "椅子上带薪神游的东星斑",  # 蓝底水印文案
}
DOT_N = 240                        # 每张图微点数量
_FONT_CANDS = (
    "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)
_f = None
for _p in _FONT_CANDS:
    if os.path.isfile(_p):
        _f = _p
        break


def _font(sz):
    try:
        if _f:
            return ImageFont.truetype(_f, sz)
    except Exception:
        pass
    return ImageFont.load_default()


def dot_positions(w, h, n=DOT_N):
    """微点阵：密钥派生的伪随机点序（固定 seed，可复现、可验证）。"""
    rng = random.Random(hashlib.sha256(SECRET.encode("utf-8")).hexdigest())
    return [(rng.uniform(0, w), rng.uniform(0, h)) for _ in range(n)]


def paper_bg(w, h):
    """纸感白 B3：底色 + 高斯细颗粒 + 纤维短线 + 中心柔光池（种子固定）。"""
    base = Image.new("RGB", (w, h), B3)
    noise = Image.effect_noise((w, h), 11).convert("L")
    base = Image.blend(base, Image.merge("RGB", (noise, noise, noise)), 0.065)
    fib = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    rng = random.Random(42)        # 纹理固定种子（与水印点序独立）
    d = ImageDraw.Draw(fib)
    for _ in range(int(w * h / 2800)):
        x, y = rng.uniform(0, w), rng.uniform(0, h)
        ln = rng.uniform(3, 10)
        ang = rng.uniform(0, math.pi)
        d.line([x, y, x + ln * math.cos(ang), y + ln * math.sin(ang)],
               fill=(212, 206, 192, rng.randint(9, 22)), width=1)
    fib = fib.filter(ImageFilter.GaussianBlur(0.6))
    base = Image.alpha_composite(base.convert("RGBA"), fib).convert("RGB")
    rad = Image.new("L", (w, h), 0)
    ImageDraw.Draw(rad).ellipse([w * -0.12, h * -0.20, w * 1.12, h * 1.04], fill=int(255 * 0.22))
    rad = rad.filter(ImageFilter.GaussianBlur(int(w * 0.24)))
    return Image.composite(Image.new("RGB", (w, h), (255, 255, 254)), base, rad)


def _wm_layer(w, h, txt):
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    f = _font(28)
    sx = int(d.textlength(txt, font=f)) + 56
    sy = 168
    for ry in range(-2, h // sy + 3):
        for rx in range(-2, w // sx + 3):
            x = rx * sx + (sx // 2 if ry % 2 else 0)
            y = ry * sy
            d.text((x, y), txt, font=f, fill=WM_COL + (ALPHA,))
    return layer.rotate(28, expand=False, resample=Image.BICUBIC)


def _dots_layer(w, h):
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    for x, y in dot_positions(w, h):
        d.ellipse([x - 1, y - 1, x + 1, y + 1], fill=(236, 234, 228, 90))
    return layer


def stamp(im, kind):
    """叠双层水印。kind: "spot"=聚光文案 / "blue"=蓝底文案。返回 RGB。"""
    rgb = im.convert("RGB")
    if not WM_ON or kind not in TXT:
        return rgb
    w, h = rgb.size
    cv = rgb.convert("RGBA")
    cv = Image.alpha_composite(cv, _dots_layer(w, h))
    cv = Image.alpha_composite(cv, _wm_layer(w, h, TXT[kind]))
    return cv.convert("RGB")


def stamp_save(im, out, quality=95):
    """按输出路径自动盖章并保存：只盖 蓝底/聚光 目录的产物，其余原样保存。"""
    s = str(out).replace("/", "\\")
    kind = None
    if "\\蓝底\\" in s:
        kind = "blue"
    elif "\\聚光\\" in s:
        kind = "spot"
    if kind is None:
        im.convert("RGB").save(out, quality=quality)
    else:
        stamp(im, kind).save(out, quality=quality)
    return out
