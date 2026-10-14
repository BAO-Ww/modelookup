# -*- coding: utf-8 -*-
"""手机 APP 图标（2026-10-14「完美手机APP」）：app/assets/icon.png → android 全套 mipmap。
Capacitor 6 模板默认图标太糊且是 Capacitor logo；本脚本用 Pillow 把产品图标
重采样写入 mipmap-{mdpi..xxxhdpi}/ic_launcher.png + ic_launcher_round.png（圆形裁剪），
并同步 adaptive icon 前景 mipmap-anydpi-v26 用的 ic_launcher_foreground.png。
全 ASCII 输出。用法：python make_apk_icons.py <icon.png> <android/app/src/main/res>"""
import os
import sys

from PIL import Image

# (目录, 方形边长)
DENSITIES = [
    ("mipmap-mdpi", 48),
    ("mipmap-hdpi", 72),
    ("mipmap-xhdpi", 96),
    ("mipmap-xxhdpi", 144),
    ("mipmap-xxxhdpi", 192),
]


def circular(img):
    """圆形裁剪（ic_launcher_round）：透明底 + 内切圆。"""
    s = img.size[0]
    mask = Image.new("L", (s * 4, s * 4), 0)
    from PIL import ImageDraw
    d = ImageDraw.Draw(mask)
    d.ellipse((0, 0, s * 4 - 1, s * 4 - 1), fill=255)
    mask = mask.resize((s, s), Image.LANCZOS)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "app/assets/icon.png"
    res = sys.argv[2] if len(sys.argv) > 2 else "mobile/app/android/app/src/main/res"
    if not os.path.isfile(src):
        sys.exit("icon source not found: %s" % src)
    img = Image.open(src).convert("RGBA")
    side = min(img.size)
    img = img.crop(((img.size[0] - side) // 2, (img.size[1] - side) // 2,
                    (img.size[0] + side) // 2, (img.size[1] + side) // 2))
    n = 0
    for sub, px in DENSITIES:
        d = os.path.join(res, sub)
        if not os.path.isdir(d):
            print("skip %s (no dir)" % sub)
            continue
        sq = img.resize((px, px), Image.LANCZOS)
        sq.save(os.path.join(d, "ic_launcher.png"))
        circular(sq).save(os.path.join(d, "ic_launcher_round.png"))
        n += 2
    # adaptive 前景（v26 圆形遮罩安全区：图案只占 2/3 居中，四边留透明）
    fg_dir = os.path.join(res, "mipmap-anydpi-v26")
    has_adaptive = os.path.isdir(fg_dir) and os.path.isfile(os.path.join(fg_dir, "ic_launcher.xml"))
    if has_adaptive:
        fgd = os.path.join(res, "mipmap-xxxhdpi")
        os.makedirs(fgd, exist_ok=True)
        canvas = Image.new("RGBA", (432, 432), (0, 0, 0, 0))
        core = img.resize((288, 288), Image.LANCZOS)
        canvas.paste(core, ((432 - 288) // 2, (432 - 288) // 2), core)
        canvas.save(os.path.join(fgd, "ic_launcher_foreground.png"))
        n += 1
        print("adaptive foreground written (432px, safe-area 2/3)")
    print("icons written: %d files from %s" % (n, src))


if __name__ == "__main__":
    main()
