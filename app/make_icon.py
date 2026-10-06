# -*- coding: utf-8 -*-
"""型号查询 图标：圆角方块 + 商务蓝渐变 + 白色放大镜 + 明黄星芒。

输出 assets/icon.ico（≤128 严格 BMP 条目，256 用 PNG）+ assets/icon.png(256)。
"""
import io
import math
import os
import struct

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "assets")

TOP = (58, 120, 240)      # #3A78F0
BOT = (27, 71, 184)       # #1B47B8
STAR = (255, 197, 61)     # #FFC53D


def grad(w, h):
    im = Image.new("RGB", (1, h))
    px = im.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(int(TOP[i] + (BOT[i] - TOP[i]) * t) for i in range(3))
    return im.resize((w, h), Image.BILINEAR)


def star(d, cx, cy, r, ratio=0.34):
    pts = []
    for i in range(8):
        a = math.pi / 4 * i - math.pi / 2
        rr = r if i % 2 == 0 else r * ratio
        pts.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    d.polygon(pts, fill=STAR)


def make(size):
    small = size < 256
    S = size * 4 if small else 1024
    k = S / 1024.0
    base = grad(S, S)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(232 * k), fill=255)
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    im.paste(base, (0, 0), mask)
    d = ImageDraw.Draw(im)

    if not small:
        d.rounded_rectangle([int(11 * k), int(11 * k), S - int(11 * k), S - int(11 * k)],
                            radius=int(222 * k), outline=(255, 255, 255, 44), width=max(2, int(5 * k)))

    # 放大镜：小尺寸整体放大 + 加粗，否则缩下去糊
    z = 1.0 if not small else (1.14 if size > 20 else 1.34)
    lw = int((56 if not small else 66) * k * z)
    cx, cy, r = int(478 * k), int(468 * k), int(200 * k * z)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 255, 255, 255), width=lw)
    hw = int((72 if not small else 86) * k * z)
    p1 = (cx + int(r * 0.70), cy + int(r * 0.70))
    p2 = (cx + int(r * 1.50), cy + int(r * 1.50))
    d.line([p1, p2], fill=(255, 255, 255, 255), width=hw)
    for (px, py) in (p1, p2):
        d.ellipse([px - hw // 2, py - hw // 2, px + hw // 2, py + hw // 2], fill=(255, 255, 255, 255))

    if size > 20:                                # ≤20px 直接不要星芒
        sr = int(104 * k) if not small else int(86 * k)
        star(d, int(786 * k), int(236 * k), sr)

    if not small:
        return im
    return im.convert("RGBa").resize((size, size), Image.LANCZOS).convert("RGBA")


def _bmp_entry(im):
    w, h = im.size
    px = im.convert("RGBA").load()
    xor = bytearray()
    for y in range(h - 1, -1, -1):
        for x in range(w):
            r, g, b, a = px[x, y]
            xor += bytes((b, g, r, a))
    andm = bytes(((w + 31) // 32) * 4 * h)
    bih = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0,
                      len(xor) + len(andm), 0, 0, 0, 0)
    return bytes(bih) + bytes(xor) + andm


def save_ico(images, path, sizes=(16, 20, 24, 32, 48, 64, 128, 256)):
    ents = []
    for s in sizes:
        im = images[s]
        if s >= 256:
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
        else:
            data = _bmp_entry(im)
        ents.append((s, data))
    head = struct.pack("<HHH", 0, 1, len(ents))
    off = 6 + 16 * len(ents)
    dirs = body = b""
    for s, data in ents:
        b = 0 if s >= 256 else s
        dirs += struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(data), off)
        body += data
        off += len(data)
    with open(path, "wb") as f:
        f.write(head + dirs + body)


def main():
    os.makedirs(OUT, exist_ok=True)
    sizes = (16, 20, 24, 32, 48, 64, 128, 256)
    images = {s: make(s) for s in sizes}
    save_ico(images, os.path.join(OUT, "icon.ico"), sizes)
    images[256].save(os.path.join(OUT, "icon.png"))

    # 自查 ICO 结构：小尺寸必须是 BMP
    d = open(os.path.join(OUT, "icon.ico"), "rb").read()
    n = struct.unpack("<HHH", d[:6])[2]
    lines = ["ICO 条目 %d 个" % n]
    bad = 0
    for i in range(n):
        w, h, _c, _r, _p, _bc, sz, ofs = struct.unpack("<BBBBHHII", d[6 + i * 16:22 + i * 16])
        s = w or 256
        fmt = "PNG" if d[ofs:ofs + 8] == b"\x89PNG\r\n\x1a\n" else "BMP"
        legal = (fmt == "PNG") == (s >= 256)
        if not legal:
            bad += 1
        lines.append("  %3dpx %s %6d 字节 %s" % (s, fmt, sz, "OK" if legal else "×不合规范"))
    lines.append("不合规范条目：%d" % bad)
    print("\n".join(lines))
    print("icon.ico / icon.png →", OUT)


if __name__ == "__main__":
    main()
