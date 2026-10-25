# -*- coding: utf-8 -*-
"""
AI9张 · 通用版（按当前型号生成，深灰高级风）
-----------------------------------------------------------------
设计（2026-09-15 用户定稿，沿用 v12 流水线）：
  · 机器像素不动：官方透明图直接裁 bbox 用（load_cut），不重抠
  · **背景 = app 内嵌 AI（CogView）点击「发布图」时实时出深灰影棚**，每机一套、
    按机型缓存到 素材/<model>/AI背景/；程序渐变只在该型号还没生背景时兜底
  · 首图：无大数字块，机器放大（约占画面高 69%）
  · 背景必须「克制」(no sparkles/streaks) 且同色系（R-B 落 ±6 冷灰族，无白底）
  · 4 种风格（studio 深灰 / spot 暗角聚光 / white 亮白 / scene 实景合成），各自独立背景缓存；
    末张九宫格总览供快速过稿（slots 指定时只出对应几张，不拼总览）
  · 文案/参数全部取自 info，图取自型号素材目录（缺则回退正面图）

依赖：同目录 core（用于字体 core.font）
"""
import os
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageStat

try:
    import pei_core as core
except Exception:
    core = None

try:
    import pei_hero as core_hero
except Exception:
    core_hero = None

W = H = 1080

# 模块级状态（generate 时写入），9 张绘制函数读取
P = {}
BG_DIR = None      # 当前型号 AI 背景目录（素材/<model>/AI背景/）
API_KEY = None     # app 内嵌 AI 的 key（ZHIPU_KEY）；发布图点击时实时出背景用
_TONE = None       # 产品主色，传给内嵌 AI 出背景做同色系微调
BG_SEED = ""      # 机型专属背景微差短语（确定性派生，保证每台机器背景各不相同）
GEN_STYLE = "studio"   # studio(深灰影棚) / spot(暗角聚光) / white(亮白影棚) / scene(实景合成) / pure(纯白底)
GEN_CAT = ""          # 当前品类（promo_scene_bg 按品类出真实家居场景用）
GEN_POP = False    # True=「暗角聚光」风格：背景加暗角聚光、机器光晕/投影加强、maxw 收安全值
BAND_MAXW = None   # pop 模式下 place_band 的 maxw 上限（≤1080 防裁切）；None=沿用各图默认
POP_FLAVOR = None  # pop 模式背景的「自由发挥」风格短语（按品类），注入 promo_studio_bg 的 prompt
TUNE = {"machine_scale": 1.0, "top_pad": 0, "font_scale": 1.0}  # 自动优化微调量（默认中性，不影响正常出图）
SHOW_SHADOW = False   # 机器下方黑影开关（用户要求去掉）；False=不画 soft_shadow

# 风格配色：直接画在背景上的文字/压暗条随风格翻转（卡片内文字不动，始终浅色）。
# 亮白影棚(white)用深色字 + 浅色压暗条；其余风格维持白字 + 深色压暗条。
TX1 = "#ffffff"    # 主标题/品牌/正文
TX2 = "#cfd8e4"    # 副标题/型号
TX3 = "#aeb4bd"    # 三级小字
TX_GOLD = "#ffe14d"  # 金色强调（始终保留，亮底上也立得住）
SCRIM_COL = (8, 10, 14)   # 顶部/底部压暗条颜色（white 时改为浅色）
SCRIM_A = 205             # 压暗条不透明度（white 时降低）


def font(sz, bold=True):
    sz = max(8, int(round(sz * TUNE.get("font_scale", 1.0))))
    if core is not None:
        try:
            return core.font(sz, bold)
        except Exception:
            pass
    try:
        from PIL import ImageFont
        name = "msyhbd.ttc" if bold else "msyh.ttc"
        try:
            return ImageFont.truetype(name, sz)
        except Exception:
            # macOS（2026-10-25）：Windows 字体名不存在，走系统中文字体
            for _p in ("/System/Library/Fonts/STHeiti Medium.ttc",
                       "/System/Library/Fonts/STHeiti Light.ttc",
                       "/System/Library/Fonts/PingFang.ttc"):
                try:
                    return ImageFont.truetype(_p, sz)
                except Exception:
                    continue
            raise
    except Exception:
        return ImageFont.load_default()


def T(dr, xy, txt, sz, fill, anchor="lm", bold=True):
    dr.text(xy, txt, font=font(sz, bold), fill=fill, anchor=anchor)


# ================= 背景（AI 按机型生成，每机一套，存 素材/<model>/AI背景/） =================
BG_DIR = None  # generate() 时写入当前型号的 AI 背景目录


def _vgrad(top, bot):
    im = Image.new("RGB", (1, H))
    dr = ImageDraw.Draw(im)
    for i in range(H):
        t = i / max(1, H - 1)
        c = tuple(int(round(top[j] + (bot[j] - top[j]) * t)) for j in range(3))
        dr.line([(0, i), (1, i)], fill=c)
    return im.resize((W, H), Image.Resampling.BILINEAR)


def _pure_bg():
    """纯白底（程序生成，确定可复现，不依赖 AI / key）。

    用极浅冷白（非纯 #FFFFFF，避免白色机器边缘融进背景），中部叠一层几乎
    不可见的中心光池给机器一点立体底光。纯白风格走这条路径，不调 AI。"""
    base = Image.new("RGB", (W, H), (248, 249, 251))
    rad = Image.new("L", (W, H), 0)
    ImageDraw.Draw(rad).ellipse(
        [W * 0.5 - 560, H * 0.5 - 560, W * 0.5 + 560, H * 0.5 + 560], fill=16)
    rad = rad.filter(ImageFilter.GaussianBlur(240))
    light = Image.new("RGB", (W, H), (255, 255, 255))
    base = Image.composite(light, base, rad)
    return base.convert("RGB")


def _fallback_bg(variant="a"):
    """兜底影棚：仅当该型号尚未生成 AI 背景（无 key / AI 失败）时使用，不主用。
    纯白风(pure)出极浅冷白；亮白风(white)出浅灰→白渐变；其余出深灰冷灰渐变。"""
    if GEN_STYLE == "pure":
        top, bot = (249, 250, 252), (253, 253, 254)    # 极浅冷白，纯白风
    elif GEN_STYLE == "white":
        top, bot = (236, 239, 243), (250, 251, 253)    # 浅灰→近白，亮白影棚
    elif variant == "char":
        top, bot = (32, 35, 41), (20, 23, 28)          # 更暗更均匀
    else:
        top, bot = (42, 46, 53), (24, 27, 32)          # 中心略亮
    base = _vgrad(top, bot).convert("RGBA")
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    cx, cy = W // 2, int(H * 0.42)
    rx, ry = W * 0.66, H * 0.60
    peak = 150 if variant == "a" else 110
    for i in range(70):
        t = i / 70
        r = rx * (1 - t * 0.92)
        a = int(peak * (1 - t) ** 1.4)
        md.ellipse([cx - r, cy - r * 0.92, cx + r, cy + r * 0.92], fill=a)
    mask = mask.filter(ImageFilter.GaussianBlur(130))
    light = Image.new("RGBA", (W, H), (118, 126, 138, 0))
    light.putalpha(mask)
    base = Image.alpha_composite(base, light)
    return base.convert("RGB")


def _neutralize(im, tol=1.5):
    """把背景整体色温拉回中性冷灰族：量 R-B 均值，偏暖则压 R 提 B，偏冷反之。
    这样「九张同色系」是**程序级保证**，不靠 AI 每次恰好出对色。
    （v13 踩坑：AI 出的一张偏暖底混进来，整组色系就毁了。）"""
    r, g, b = ImageStat.Stat(im).mean
    rb = r - b
    if abs(rb) <= tol:
        return im
    k = rb / 2.0
    ch = im.split()
    R = ch[0].point(lambda v: max(0, min(255, int(round(v - k)))))
    B = ch[2].point(lambda v: max(0, min(255, int(round(v + k)))))
    return Image.merge("RGB", (R, ch[1], B))


def _seed_phrase(info):
    """由机型确定性派生一段「背景微调」短语，保证每台机器背景各不相同，
    但都停留在深灰冷灰高级影棚族（不跑色）。"""
    import hashlib
    s = "%s %s %s" % (info.get("brand", "") or "", info.get("model", "") or "",
                      info.get("cat", "") or "")
    h = int(hashlib.md5(s.encode("utf-8")).hexdigest(), 16)
    side = ("居中", "略偏左", "略偏右", "略偏上")[h % 4]
    tint = ("冷蓝微差", "中性冷灰", "冷青微差")[h // 4 % 3]
    pool = ("标准椭圆", "偏宽椭圆", "偏窄椭圆")[h // 16 % 3]
    return ("本机型专属影棚：椭圆光池%s、%s，整体%s，仍保持深灰冷灰高级感，不要彩色"
            % (side, pool, tint))


_POP_FLAVOR_TABLE = [
    ("冰箱", "为对开门/多门冰箱设计：冷调、带高级厨房空间暗示的奢华深色大理石影棚"),
    ("洗衣", "为滚筒/波轮洗衣机设计：中性偏冷静、带居家洗衣空间感的奢华深色背景"),
    ("烟机", "为厨房烟机设计：偏暖的深色拉丝金属质感奢华影棚，电影光"),
    ("热水", "为燃气/电热水器设计：冷蓝调科技奢华感深色背景，干净利落"),
    ("灶",   "为嵌入式燃气灶设计：暖灰调、带高级灶台石材质感的深色背景"),
    ("冷柜", "为冷柜设计：冰蓝冷调、极简科技感的深色背景，干净通透"),
    ("集成灶", "为集成灶设计：暖灰调、带高级黑色岩板材质的深色背景，电影光"),
    ("洗碗", "为洗碗机设计：冷蓝调、玻璃质感科技奢华深色背景"),
    ("空调", "为空调设计：清新冷调、现代简约的深色影棚，极简留白高级感"),
    ("冰吧", "为冰吧设计：冰蓝通透玻璃质感、极简科技深色背景"),
    ("酒柜", "为酒柜设计：深酒红木质酒窖调、奢华深色背景"),
    ("电视", "为电视设计：深灰科技客厅影棚，电影级柔光"),
]


def _pop_flavor(info):
    """按品类给 pop 模式背景挑一句「自由发挥」风格短语（注入 promo_studio_bg prompt），
    让每台机器的背景更有高级感、各不相同，但仍保持深灰冷灰族（不跑色）。"""
    cat = (info.get("cat") or "") + " " + (info.get("kind") or "") + " " + (info.get("model") or "")
    for kw, fl in _POP_FLAVOR_TABLE:
        if kw in cat:
            return fl
    return "为家电产品设计：奢华深色影棚，电影级柔光，绝对无白底、无文字、无产品"


# 亮白影棚风格的背景「自由发挥」短语：明亮、干净、浅灰到纯白渐变。
WHITE_FLAVOR = ("明亮白色极简产品摄影影棚背景，浅灰到纯白的柔和渐变，高明度、干净通透，"
                "顶部柔光形成一道极淡的光池，绝对无阴影硬边、无图案、无纹理、无边框、"
                "无文字、无产品、无人物")


def prep_bg(key):
    """当前型号背景 = app 内嵌 AI（CogView）实时生成，按机型+风格缓存复用，不主用程序渐变。

    优先级：① 本模块缓存 studio_<key>_<style>.jpg（之前点对应风格已生成落地）
            ② 内嵌 AI 实时生成（按 GEN_STYLE 选生成器）：
               - studio/spot/white → promo_studio_bg（spot 注入品类 flavor；white 走 bright 亮白）
               - scene             → promo_scene_bg（按品类出真实家居场景，dark 保证白字可读）
            ③ 兜底：程序渐变（无 key / AI 失败）

    不论哪条路径，最后都过一道色温归一化，保证同色系、无白底（亮白风不压暗）。

    ⚠️ 缓存名含 GEN_STYLE：每台机器每个风格各一套独立底图。这是修「暗角聚光」曾复用
    「深灰影棚」AI 底图、导致两者看起来没区别的关键 —— 风格必须进缓存键。
    """
    if BG_DIR is not None:
        # ① 缓存（缓存名带 风格 + seed/tone 短哈希：机型/主色/风格一变就换新背景）
        import hashlib as _hl
        _h = _hl.md5(("|%s|%s|%s" % (BG_SEED, _TONE, GEN_STYLE)).encode("utf-8")).hexdigest()[:8]
        cache = Path(BG_DIR) / ("studio_%s_%s_%s.jpg" % (key, GEN_STYLE, _h))
        if cache.exists():
            try:
                return _finalize_bg(Image.open(cache).convert("RGB"))
            except Exception:
                pass
        # 纯白风：程序直接生成纯白底，不调 AI（确定、不依赖 key、无联网）
        if GEN_STYLE == "pure":
            return _finalize_bg(_pure_bg())
        # ② 内嵌 AI 实时生成（点击发布图时走这里）
        if core_hero is not None and API_KEY is not None:
            try:
                if GEN_STYLE == "scene":
                    im = core_hero.promo_scene_bg(GEN_CAT, API_KEY, str(BG_DIR),
                                                 log=None, dark=True, tone=_TONE)
                else:
                    flavor = (POP_FLAVOR if GEN_STYLE == "spot"
                              else WHITE_FLAVOR if GEN_STYLE == "white" else None)
                    im = core_hero.promo_studio_bg(key, API_KEY, str(BG_DIR),
                                                   tone=_TONE, seed=BG_SEED,
                                                   flavor=flavor, bright=(GEN_STYLE == "white"))
                if im is not None:
                    try:
                        im.convert("RGB").save(str(cache), quality=92)
                    except Exception:
                        pass
                    return _finalize_bg(im.convert("RGB"))
            except Exception:
                pass
    # ③ 兜底程序渐变
    return _finalize_bg(_fallback_bg(key))


def _finalize_bg(im):
    """背景最终处理：pop 模式加暗角聚光，再 cover 到画布并色温归一化。"""
    if GEN_POP:
        im = pop_bg(im.convert("RGB"))
    return _neutralize(cover_fit(im, W, H))


def radial_mask(w, h, cx, cy, ri, ro):
    """中心白(cx,cy)、四周黑(ro 外)的径向渐变遮罩。"""
    import math
    m = Image.new("L", (w, h), 0)
    px = m.load()
    for y in range(h):
        for x in range(w):
            d = math.hypot(x - cx, y - cy)
            if d <= ri:
                v = 255
            elif d >= ro:
                v = 0
            else:
                v = int(255 * (ro - d) / (ro - ri))
            px[x, y] = v
    return m


def pop_bg(bg):
    """暗角 + 中心聚光：四周压暗、机器所在中心区提亮，让机器更显眼。
    仅在 GEN_POP 模式下由 _finalize_bg 调用（pop 模式专用）。"""
    w, h = bg.size
    cx, cy = w // 2, int(h * 0.55)
    ri, ro = int(w * 0.30), int(w * 0.80)
    m = radial_mask(w, h, cx, cy, ri, ro)
    spot = bg.point(lambda p: min(255, int(p * 1.16)))   # 中心略亮
    vig = bg.point(lambda p: int(p * 0.40))              # 四周压暗
    return Image.composite(spot, vig, m)


# ================= 通用工具 =================
def cover_fit(img, w, h):
    s = max(w / img.width, h / img.height)
    im = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), Image.LANCZOS)
    return im.crop(((im.width - w) // 2, (im.height - h) // 2,
                    (im.width - w) // 2 + w, (im.height - h) // 2 + h))


def prep_photo(path, focus=0.5, xoff=0.5, sharpen=True):
    im = Image.open(path).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
    x = int((im.width - W) * xoff); y = int((im.height - H) * focus)
    im = im.crop((x, y, x + W, y + H))
    if sharpen:
        im = im.filter(ImageFilter.UnsharpMask(radius=2, percent=110, threshold=3))
    return im


def load_cut(path, matting=True):
    """裁出产品主体。
    透明 PNG -> 直接 bbox 裁（官方图多自带透明）。
    不透明图 -> 若背景是纯白（官方商品图常见），做本地 chroma-key 抠图
    （从四边 flood fill 近白像素置透明 + 边缘轻微羽化），可移植、无 API 依赖；
    彩色/渐变底则保留（由 GLM-4V 自检拦截误用）。"""
    im = Image.open(path)
    # ★ 带透明但非 RGBA 的模式必须**先**转 RGBA，否则下面 convert("RGB") 会把透明丢掉：
    #   · P（调色板）把透明存在 tRNS 里 → convert("RGB") 后透明像素退化成调色板色（多为纯黑）
    #     → 机器外面套一圈黑方块（2026-09-21 用户报「好是好了，怎么有黑框了」）。
    #   · LA 同理（灰度+alpha）。
    #   实测：XQB100-BS618 的 01.png 是 P 模式，convert("RGBA") → alpha=(0,255)（本就是抠好的图），
    #   convert("RGB") → 四角变 (0,0,0) 纯黑。
    if im.mode in ("P", "PA", "LA", "La"):
        im = im.convert("RGBA")
    if im.mode == "RGBA":
        if im.split()[3].getextrema()[0] < 255:
            bb = im.getbbox()
            return im.crop(bb) if bb else im
        im = im.convert("RGB")
    else:
        im = im.convert("RGB")
    if matting and _looks_white_bg(im):
        return _white_matte(im)
    bb = im.getbbox()
    im = im.crop(bb) if bb else im
    # ★ 必须返回 RGBA：下游 img1~img8 全部用 cv.alpha_composite(pf, (px, py))，
    #   而 Pillow 12.3 的 alpha_composite(dest=...) 要求叠加图为 RGBA，
    #   返回 RGB 会报 ValueError: images do not match（**与尺寸越界无关**）。
    #   踩坑（2026-09-21）：源图「不透明且背景非近白」时这里返回 RGB，
    #   导致「发布图」卡在「正在合成 1/6 首图 26%」像卡死。
    return im if im.mode == "RGBA" else im.convert("RGBA")


def _looks_white_bg(im, thresh=232, ratio=0.92):
    w, h = im.size
    px = im.load()
    edge = []
    for x in range(0, w, max(1, w // 40)):
        edge.append(px[x, 0]); edge.append(px[x, h - 1])
    for y in range(0, h, max(1, h // 40)):
        edge.append(px[0, y]); edge.append(px[w - 1, y])
    if not edge:
        return False
    white = sum(1 for r, g, b in edge if min(r, g, b) >= thresh)
    return white / len(edge) >= ratio


def _white_matte(im):
    from collections import deque
    rgba = im.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()

    def near(c):
        return min(c[0], c[1], c[2]) >= 232
    vis = [[False] * w for _ in range(h)]
    dq = deque()
    for x in range(w):
        for y in (0, h - 1):
            if near(px[x, y]):
                dq.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if near(px[x, y]):
                dq.append((x, y))
    while dq:
        x, y = dq.popleft()
        if vis[y][x]:
            continue
        vis[y][x] = True
        if not near(px[x, y]):
            continue
        c = px[x, y]
        px[x, y] = (c[0], c[1], c[2], 0)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h and not vis[ny][nx]:
                dq.append((nx, ny))
    # 边缘轻微羽化，避免硬白边
    a = rgba.split()[3].filter(ImageFilter.GaussianBlur(0.8))
    rgba.putalpha(a)
    bb = rgba.getbbox()
    return rgba.crop(bb) if bb else rgba


def dim(cv, box, color=(0, 0, 0), alpha=90, radius=0):
    x0, y0, x1, y1 = box
    lay = Image.new("RGBA", (x1 - x0, y1 - y0), color + (alpha,))
    if radius:
        m = Image.new("L", (x1 - x0, y1 - y0), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, x1 - x0 - 1, y1 - y0 - 1], radius, fill=255)
        lay.putalpha(m.point(lambda v: int(v * alpha / 255)))
    cv.alpha_composite(lay, (x0, y0))


def vgrad_overlay(cv, top_alpha=0, bot_alpha=0, band=260, color=None):
    if color is None:
        color = (244, 246, 249) if GEN_STYLE in ("white", "pure") else (8, 10, 14)
    if top_alpha:
        lay = Image.new("RGBA", (W, band), color + (0,)); px = lay.load()
        for y in range(band):
            a = int(top_alpha * (1 - y / band))
            for x in range(W): px[x, y] = color + (a,)
        cv.alpha_composite(lay, (0, 0))
    if bot_alpha:
        lay = Image.new("RGBA", (W, band), color + (0,)); px = lay.load()
        for y in range(band):
            a = int(bot_alpha * (y / band))
            for x in range(W): px[x, y] = color + (a,)
        cv.alpha_composite(lay, (0, H - band))


def top_scrim(cv, solid=240, a0=185, fade=210, color=None):
    if color is None:
        color = (244, 246, 249) if GEN_STYLE in ("white", "pure") else (8, 10, 14)
    dim(cv, (0, 0, W, solid), color, a0)
    lay = Image.new("RGBA", (W, fade), color + (0,)); px = lay.load()
    for y in range(fade):
        a = int(a0 * (1 - y / fade))
        for x in range(W): px[x, y] = color + (a,)
    cv.alpha_composite(lay, (0, solid))


def soft_shadow(cv, px, py, pw, ph, alpha=90, blur=26, drop=20, w=0.9, col=(10, 10, 12)):
    if not SHOW_SHADOW:               # 用户要求去掉机器下方黑影
        return
    if GEN_STYLE == "pure":           # 纯白底：机器用浅灰投影才立体（深投影会成黑圈）
        col = (150, 155, 162); alpha = min(78, alpha)
    elif GEN_POP:                     # 机器更显眼：投影更重、更柔、更大
        alpha = min(215, alpha + 25); blur = min(60, blur + 8); drop = min(40, drop + 8)
    sh = Image.new("RGBA", (max(pw, 8), 110), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([pw * (0.5 - w / 2), 0, pw * (0.5 + w / 2), 64], fill=col + (alpha,))
    sh = sh.filter(ImageFilter.GaussianBlur(blur))
    cv.alpha_composite(sh, (px, py + ph - drop))


def halo(cv, px, py, pw, ph, strength=26, blur=58, m=190, col=(255, 255, 255)):
    if GEN_POP:                       # 机器更显眼：光晕更强、更柔、边距更大
        strength = min(96, strength + 14); blur = min(130, blur + 14); m = min(300, m + 14)
    lay = Image.new("RGBA", (pw + 2 * m, ph + 2 * m), (0, 0, 0, 0))
    ImageDraw.Draw(lay).ellipse([m, m, pw + m, ph + m], fill=col + (strength,))
    lay = lay.filter(ImageFilter.GaussianBlur(blur))
    cv.alpha_composite(lay, (px - m, py - m))


def check(dr, cx, cy, r, col):
    dr.ellipse([cx - r, cy - r, cx + r, cy + r], fill=col)
    dr.line([cx - r * 0.45, cy + r * 0.02, cx - r * 0.12, cy + r * 0.36,
             cx + r * 0.5, cy - r * 0.34], fill=(255, 255, 255), width=max(3, r // 4), joint="curve")


def is_wide(prod):
    return prod.width / prod.height >= 1.4


def place_band(cv, prod, top, bot, maxw=1000):
    scale = TUNE.get("machine_scale", 1.0)
    top = top + int(TUNE.get("top_pad", 0))
    ar = prod.width / prod.height
    band = bot - top
    if is_wide(prod):
        w = maxw; h = w / ar
        if h > band:
            h = band; w = h * ar
    else:
        h = band; w = h * ar
        if w > maxw:
            w = maxw; h = w / ar
    # pop 模式安全上限：防宽机型 maxw 超过画布被裁切（如旧 maxw=1130>1080）
    if BAND_MAXW is not None and w > BAND_MAXW:
        w = BAND_MAXW; h = w / ar
        if h > band:
            h = band; w = h * ar
    w, h = int(w * scale), int(h * scale)
    pf = prod.resize((w, h), Image.LANCZOS)
    px = (W - w) // 2; py = int(top + (band - h) / 2)
    return pf, px, py


def chip_row(cv, dr, items, y0, ch=112, cw=336, g=18, col=(255, 225, 90),
             fill=(8, 10, 14), alpha=212, vsz=36, lsz=19):
    tot = len(items) * cw + (len(items) - 1) * g
    sx = (W - tot) // 2
    for i, (v, k) in enumerate(items):
        x = sx + i * (cw + g)
        dim(cv, (x, y0, x + cw, y0 + ch), fill, alpha, radius=18)
        dr.rounded_rectangle([x, y0, x + cw, y0 + ch], 18, outline=col + (190,), width=2)
        dr.rounded_rectangle([x + 18, y0 + 22, x + 30, y0 + ch - 22], 6, fill=col)
        _s = vsz if dr.textlength(v, font=font(vsz, True)) <= cw - 76 else 22
        T(dr, (x + 46, y0 + ch // 2 - 13), v, _s, "#ffe14d", "lm")
        T(dr, (x + 46, y0 + ch // 2 + 24), k, lsz, "#e6ebf2", "lm")


def save(cv, p):
    cv.convert("RGB").save(p, quality=96)
    return p


# ================= 9 张 =================
def img1(front, out):                 # 首图：干净版——不放大数字/价格块，机器放大占满
    cv = prep_bg("a").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    # 顶栏：成色 + 全国联保
    dim(cv, (0, 0, W, 52), SCRIM_COL, SCRIM_A)
    T(dr, (40, 27), "全新未拆封", 27, TX1, "lm")
    T(dr, (W - 40, 27), "全国联保", 24, TX1, "rm")
    # 品牌 + 型号（左上角两行，简洁）
    T(dr, (40, 92), P['ben'], 24, TX2, "lm")
    bx = 40 + int(dr.textlength(P['ben'], font=font(24, True))) + 12
    T(dr, (bx, 92), P['brand'], 32, TX1, "lm")
    _line3 = " · ".join([x for x in (P['model'], P['color'], P['cat']) if x])
    T(dr, (40, 132), _line3, 19, TX3, "lm")
    # 机器放大：占满中间，上下各留文字带（机器顶边到型号行、机器底边到发货地各 ≥20px）
    pf, px, py = place_band(cv, front, 150, 994, maxw=1130)
    halo(cv, px, py, pf.width, pf.height, strength=34, blur=60, m=190, col=(205, 218, 238))
    soft_shadow(cv, px, py, pf.width, pf.height, alpha=135, blur=32, drop=16, w=0.98)
    cv.alpha_composite(pf, (px, py))
    # 底部：发货地（不放价格块）
    T(dr, (W // 2, 1034), f"可发货地区：{P['shipping']}", 21, TX1, "mm")
    return save(cv, out / "1_首图.jpg")


def img2(front, out):                 # 机器图（深色，同色系）
    cv = prep_bg("char").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    T(dr, (W // 2, 76), f"{P['brand']} {P['cat']}", 40, TX1, "mm")
    sub = (P['color'] + " · " if P['color'] else "") + "全新未拆封"
    T(dr, (W // 2, 122), sub, 20, TX2, "mm")
    pf, px, py = place_band(cv, front, 156, 826, maxw=1010)
    halo(cv, px, py, pf.width, pf.height, strength=32, blur=60, m=190, col=(210, 222, 240))
    soft_shadow(cv, px, py, pf.width, pf.height, alpha=120, blur=34, drop=26, w=0.98)
    cv.alpha_composite(pf, (px, py))
    specs = P['specs']; n = len(specs); mg = 34; g = 16
    cw = (W - 2 * mg - (n - 1) * g) // n; ch = 118; y0 = 848
    for i, (v, k) in enumerate(specs):
        x = mg + i * (cw + g)
        dim(cv, (x, y0, x + cw, y0 + ch), (20, 24, 30), 222, radius=18)
        dr.rounded_rectangle([x, y0, x + cw, y0 + ch], 18, outline=(150, 162, 180, 200), width=2)
        _s = 27 if dr.textlength(v, font=font(27, True)) <= cw - 20 else 20
        T(dr, (x + cw // 2, y0 + 42), v, _s, "#ffffff", "mm")
        T(dr, (x + cw // 2, y0 + 84), k, 17, "#aab3c0", "mm")
    T(dr, (W // 2, 1026), f"{P['brand']} {P['model']} · 全新未拆封", 23, TX2, "mm")
    return save(cv, out / "2_机器图.jpg")


def img3(angle, out):                 # 立体角度
    cv = prep_bg("char").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    T(dr, (W // 2, 74), P['angle_title'], 38, TX1, "mm")
    T(dr, (W // 2, 118), P['angle_sub'], 20, TX2, "mm")
    pf, px, py = place_band(cv, angle, 158, 852, maxw=1010)
    halo(cv, px, py, pf.width, pf.height, strength=30, blur=60, m=190, col=(210, 222, 240))
    soft_shadow(cv, px, py, pf.width, pf.height, alpha=120, blur=34, drop=24, w=0.98)
    cv.alpha_composite(pf, (px, py))
    bx0, by0, bw_, bh_ = 56, 886, W - 112, 116
    dim(cv, (bx0, by0, bx0 + bw_, by0 + bh_), (10, 12, 16), 224, radius=18)
    dr.rounded_rectangle([bx0, by0, bx0 + bw_, by0 + bh_], 18, outline=(255, 225, 90, 140), width=2)
    _s1 = 25 if dr.textlength(P['dim1'], font=font(25, True)) <= bw_ - 60 else 21
    T(dr, (W // 2, by0 + 42), P['dim1'], _s1, "#ffffff", "mm")
    _s2 = 21 if dr.textlength(P['dim2'], font=font(21, True)) <= bw_ - 60 else 18
    T(dr, (W // 2, by0 + 82), P['dim2'], _s2, "#bcc6d2", "mm")
    T(dr, (W // 2, 1046), f"{P['brand']} {P['model']} · {P['tag']} · 全新未拆封", 22, TX2, "mm")
    return save(cv, out / "3_立体角度.jpg")


def img4(front, out):                 # 核心参数
    cv = prep_bg("a").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    vgrad_overlay(cv, top_alpha=120, bot_alpha=140, band=200)
    T(dr, (60, 92), "核心参数", 42, TX1, "lm")
    T(dr, (60, 146), "参数过硬 · 买得放心", 22, TX3, "lm")
    T(dr, (W - 60, 118), P['model'], 20, TX3, "rm")
    cards = P['cards']
    pf, px, py = place_band(cv, front, 200, 946, maxw=420)
    px = 30 + (420 - pf.width) // 2
    halo(cv, px, py, pf.width, pf.height, strength=30, blur=58, m=185, col=(205, 220, 245))
    soft_shadow(cv, px, py, pf.width, pf.height, alpha=120, blur=30, drop=16, w=0.9)
    cv.alpha_composite(pf, (px, py))
    cw, ch, gx, gy = 272, 190, 18, 24
    x0 = 490; y0 = 244
    for i, (v, k, ic) in enumerate(cards):
        r, c = divmod(i, 2)
        x = x0 + c * (cw + gx); y = y0 + r * (ch + gy)
        dim(cv, (x, y, x + cw, y + ch), (10, 14, 22), 208, radius=16)
        dr.rounded_rectangle([x, y, x + cw, y + ch], 16, outline=(180, 195, 215, 120), width=2)
        dr.rounded_rectangle([x + 20, y + 26, x + 34, y + ch - 26], 7, fill=ic)
        _s = 34 if dr.textlength(v, font=font(34, True)) <= cw - 100 else 22
        T(dr, (x + 54, y + ch // 2 - 20), v, _s, "#ffffff", "lm")
        T(dr, (x + 54, y + ch // 2 + 24), k, 19, "#a9b3c1", "lm")
    _s1 = 21 if dr.textlength(P['pl1'], font=font(21, True)) <= W - 120 else 18
    _s2 = 19 if dr.textlength(P['pl2'], font=font(19, True)) <= W - 120 else 16
    T(dr, (W // 2, 1006), P['pl1'], _s1, TX2, "mm")
    T(dr, (W // 2, 1048), P['pl2'], _s2, TX3, "mm")
    return save(cv, out / "4_核心参数.jpg")


def img5(openpng, out):               # 开门大容量
    cv = prep_bg("char").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    top_scrim(cv, solid=206, a0=170, fade=184)
    T(dr, (W // 2, 92), P['o_title'], 50, "#ffffff", "mm")
    T(dr, (W // 2, 152), P['o_sub'], 23, "#d7dee7", "mm")
    pf, px, py = place_band(cv, openpng, 190, 878, maxw=880)
    halo(cv, px, py, pf.width, pf.height, strength=30, blur=60, m=190, col=(205, 220, 240))
    soft_shadow(cv, px, py, pf.width, pf.height, alpha=130, blur=32, drop=18, w=0.98)
    cv.alpha_composite(pf, (px, py))
    chip_row(cv, dr, P['o_chips'], y0=898, ch=114)
    T(dr, (W // 2, 1052), P['o_foot'], 24, "#ffe14d", "mm")
    return save(cv, out / "5_开门大容量.jpg")


def img6(scene, front, out):          # 实拍场景：有实景照居中铺底；无则直接摆产品，保证九宫格每格都有机器
    # 文件名仍沿用 P['r_title']（如「产品实拍」），保证图片库分组与文件名稳定。
    # ⚠️ 图面本身**不写顶部标题**（用户 2026-09-21 明确：去掉「产品实拍」标题 + 副标题）：
    #   · 连带去掉那块深灰圆角底板（它只是标题的衬底）；
    #   · 也去掉 top_scrim —— 它唯一的用途就是给标题衬底，而它在**亮底风格（white/pure）**
    #     用的是浅色 (244,246,249)、a0 高达 186，会在画面顶部糊出一层「白雾」，
    #     正是用户反馈的「怎么会有一层白的在上面盖着」。标题没了它就没有存在意义。
    scene_name = (P.get('r_title') or "实拍场景").strip()
    cv = prep_bg("char").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    if scene is not None and getattr(scene, "mode", None) == "RGBA" and scene.getbbox():
        # 实景照：居中裁切铺底（cover_fit 不变形，主体居中，避免挤压导致「机器偏一边」）
        bw = cover_fit(scene.convert("RGB"), W, H)
        cv.alpha_composite(bw.convert("RGBA"), (0, 0))
        dr = ImageDraw.Draw(cv)
    else:
        # 无实景照：直接把产品当主体摆上影棚底，避免这一格只剩文字（看起来像空白）
        pf, px, py = place_band(cv, front, 196, 852, maxw=900)
        halo(cv, px, py, pf.width, pf.height, strength=30, blur=60, m=190, col=(210, 222, 240))
        soft_shadow(cv, px, py, pf.width, pf.height, alpha=130, blur=32, drop=18, w=0.98)
        cv.alpha_composite(pf, (px, py))
    # 只保留底部轻渐变（托住底部卖点/型号），顶部不再有任何压暗/提亮层
    vgrad_overlay(cv, top_alpha=0, bot_alpha=195, band=310)
    dr = ImageDraw.Draw(cv)
    chip_row(cv, dr, P['r_chips'], y0=876, ch=114)
    T(dr, (W // 2, 1030), P['r_foot'], 24, "#ffe14d", "mm")
    return save(cv, out / ("6_%s.jpg" % scene_name))


def prep_photo_from_img(im):
    return im


def img7(front, out):                  # 能效静音
    cv = prep_bg("char").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    T(dr, (W // 2, 100), P['e_title'], 48, "#ffffff", "mm")
    T(dr, (W // 2, 162), P['e_sub'], 23, "#dfe4ec", "mm")
    pf, px, py = place_band(cv, front, 226, 850, maxw=1000)
    halo(cv, px, py, pf.width, pf.height, strength=32, blur=60, m=190, col=(210, 222, 245))
    soft_shadow(cv, px, py, pf.width, pf.height, alpha=120, blur=30, drop=18, w=0.98, col=(16, 18, 24))
    cv.alpha_composite(pf, (px, py))
    chip_row(cv, dr, P['e_chips'], y0=878, ch=116)
    T(dr, (W // 2, 1046), P['e_foot'], 24, "#ffe14d", "mm")
    return save(cv, out / "7_能效静音.jpg")


def img8(front, out):                  # 品质细节
    cv = prep_bg("char").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    vgrad_overlay(cv, top_alpha=130, bot_alpha=170, band=250)
    T(dr, (W // 2, 98), "品质细节", 46, "#ffffff", "mm")
    pf, px, py = place_band(cv, front, 178, 838, maxw=1000)
    halo(cv, px, py, pf.width, pf.height, strength=30, blur=58, m=185, col=(205, 220, 245))
    soft_shadow(cv, px, py, pf.width, pf.height, alpha=120, blur=30, drop=18, w=0.98)
    cv.alpha_composite(pf, (px, py))
    cw, ch, g = 336, 118, 18
    tot = 3 * cw + 2 * g; sx = (W - tot) // 2; y0 = 872
    for i, (v, d, ic) in enumerate(P['d_feats']):
        x = sx + i * (cw + g)
        dim(cv, (x, y0, x + cw, y0 + ch), (8, 13, 20), 216, radius=18)
        dr.rounded_rectangle([x, y0, x + cw, y0 + ch], 18, outline=ic + (225,), width=2)
        dr.rounded_rectangle([x + 18, y0 + 24, x + 31, y0 + ch - 24], 6, fill=ic)
        _s = 29 if dr.textlength(v, font=font(29, True)) <= cw - 62 else 21
        T(dr, (x + 48, y0 + 42), v, _s, "#ffffff", "lm")
        T(dr, (x + 48, y0 + 82), d, 18, "#b3bfcd", "lm")
    T(dr, (W // 2, 1046), P['d_foot'], 22, "#cfdae6", "mm")
    return save(cv, out / "8_品质细节.jpg")


def img9(out):                         # 交易保障
    cv = prep_bg("a").convert("RGBA")
    dr = ImageDraw.Draw(cv)
    vgrad_overlay(cv, top_alpha=150, bot_alpha=95, band=260)
    T(dr, (W // 2, 96), "正品交易保障", 50, TX1, "mm")
    T(dr, (W // 2, 158), f"{P['brand']}官方授权 · 全国联保 · 安心购物", 23, TX2, "mm")
    bx0, by0, bw_, bh_ = 100, 214, W - 200, 660
    dim(cv, (bx0, by0, bx0 + bw_, by0 + bh_), (8, 9, 12), 172, radius=28)
    dr.rounded_rectangle([bx0, by0, bx0 + bw_, by0 + bh_], 28, outline=(255, 225, 140, 110), width=2)
    items = [f"{P['brand']}官方正品，全新未拆封，全国联保",
             "品质保证，放心购买，售后无忧",
             "支持专业送货安装，当地售后师傅上门安装",
             "先验后签收，破损拒收",
             "让您买得放心，用得安心！"]
    iy = 296; lh = 112
    for i, t in enumerate(items):
        y = iy + i * lh
        check(dr, bx0 + 66, y, 24, (72, 205, 142))
        tl = dr.textlength(t, font=font(27, True))
        _s = 27 if tl <= 720 else max(19, int(27 * 720 / tl))
        T(dr, (bx0 + 118, y), t, _s, "#ffffff", "lm")
        if i < len(items) - 1:
            dr.line([bx0 + 66, y + 56, bx0 + bw_ - 52, y + 56], fill=(255, 255, 255, 46), width=1)
    sr = 74; sx, sy = W - 214, 800
    seal = Image.new("RGBA", (sr * 2, sr * 2), (0, 0, 0, 0)); sd = ImageDraw.Draw(seal)
    sd.ellipse([7, 7, sr * 2 - 7, sr * 2 - 7], outline=(226, 76, 66, 215), width=7)
    sd.ellipse([26, 26, sr * 2 - 26, sr * 2 - 26], outline=(226, 76, 66, 170), width=3)
    T(sd, (sr, sr - 24), "安心", 34, (232, 86, 74, 240), "mm")
    T(sd, (sr, sr + 24), "交易", 34, (232, 86, 74, 240), "mm")
    seal = seal.rotate(-14, expand=True)
    cv.alpha_composite(seal, (sx - seal.width // 2, sy - seal.height // 2))
    T(dr, (W // 2, 998), f"可发货地区：{P['shipping']}", 23, "#ffe14d", "mm")
    return save(cv, out / "9_交易保障.jpg")


def contact_sheet(out, paths, cols=3):
    """把本次实际生成的图拼一张总览（自适应网格：6 张→2×3，9 张→3×3）。
    paths 为本次生成的图片路径列表（不含总览本身）。"""
    paths = [Path(p) for p in paths]
    n = len(paths)
    if n == 0:
        return None
    rows = (n + cols - 1) // cols
    cell = 360; gap = 14; pad = 24
    S = pad * 2 + cell * cols + gap * (cols - 1)
    Hh = pad * 2 + cell * rows + gap * (rows - 1)
    cv = Image.new('RGB', (S, Hh), (20, 22, 26)); dr = ImageDraw.Draw(cv)
    for i, p in enumerate(paths):
        im = Image.open(p).convert('RGB').resize((cell, cell), Image.LANCZOS)
        r, c = divmod(i, cols); x = pad + c * (cell + gap); y = pad + r * (cell + gap)
        cv.paste(im, (x, y))
        dr.rectangle([x - 1, y - 1, x + cell, y + cell], outline=(60, 66, 74), width=2)
        dr.rectangle([x + 8, y + 8, x + 40, y + 38], fill=(10, 11, 14))
        T(dr, (x + 24, y + 23), str(i + 1), 20, (255, 255, 255), "mm")
    p = Path(out) / "0_九宫格总览.jpg"
    cv.save(p, quality=94)
    return p


# ================= 素材挑选 =================
def _is_cutout(f, min_side=220):
    """是否为可用的「产品抠图」：转 RGBA 后真有透明像素、尺寸合理、不是长条精灵图。
    带底图（全不透明的 P/JPG）→ 一律不算，避免被当成抠图摆上影棚底变成「方块」。

    2026-09-15 热水器踩坑：09.png 是 6×4074 的精灵长条，07.png 是带底调色板图，
    被误选进 立体角度/开门大容量 导致「不是机器」或「空白」格 —— 这里直接淘汰。"""
    try:
        im = Image.open(f)
    except Exception:
        return False
    w, h = im.size
    if w < min_side or h < min_side:
        return False
    ar = w / h
    if ar < 0.18 or ar > 5.5:          # 精灵条 / 超长图（如 6×4074）淘汰
        return False
    try:
        rgba = im.convert("RGBA")
        a0 = rgba.getextrema()[3][0]
    except Exception:
        return False
    return a0 < 255                     # 真有透明像素才算抠图


def _is_junk_cut(f):
    """透明底「非机器图」守门（2026-10-24）：实现统一在 pei_core.is_junk_cut，
    选图规则层与 AI 视觉选图候选池共用同一份，口径绝不分叉。"""
    try:
        return core.is_junk_cut(f)
    except Exception:
        return False


def _src_tiers(src_dir):
    """来源层（2026-10-24 用户定稿：官网 > 欣盛 > 其他）。
    读 src_dir/info.json 的 srcmap（文件名→来源，pubwork.work 抓取时写入），
    没有逐图记录的用 site 兜底（haier/casarte/leader=官网 0、xinsheng=欣盛 1、其余=2）。
    返回 tier(name) 函数；info 缺失时恒 0（行为与旧版一致）。"""
    try:
        info = json.loads((Path(src_dir) / "info.json").read_text(encoding="utf-8"))
    except Exception:
        return lambda name: 0
    sm = info.get("srcmap") if isinstance(info.get("srcmap"), dict) else {}
    site = str(info.get("site") or "")
    base = 0 if site in ("haier", "casarte", "leader") else (1 if site == "xinsheng" else 2)
    m = {}
    for k, v in sm.items():
        v = str(v)
        m[str(k)] = 0 if v == "官网" else (1 if v == "欣盛" else 2)

    def tier(name):
        n = str(name).replace("\\", "/")
        if n.startswith("官网/"):              # 2026-10-24 A 方案：子文件夹即来源
            return 0
        if n.startswith("欣盛/"):
            return 1
        if n.startswith("其他/"):
            return 2
        if n in m:
            return m[n]
        return m.get(n.rsplit("/", 1)[-1], base)
    return tier


def _is_photo(f, min_side=420):
    """实景照（带底也行）：尺寸不能太小、不能超长（剔除 750×4604 这类规格长图）。"""
    try:
        im = Image.open(f)
    except Exception:
        return False
    w, h = im.size
    if w < min_side or h < min_side:
        return False
    ar = w / h
    if ar < 0.4 or ar > 2.6:           # 规格长图剔除
        return False
    return True


# 品类正面目标宽高比（量 alpha 非透明 bbox）：立式机器关门正面 ≈0.65 瘦长，
# 横式机器（油烟机/灶具/电视等）≈1.4 扁宽。开门图门往外甩 bbox 变宽（≥1.0）、
# 特写/顶板异形 —— 靠 ar 就能跟关门正面分开（XQG100-28E 欣盛图实锤：
# 开门 02/03 ar=1.05/1.20，关门正面 06 ar=0.70，老规则按文件名顺序取第一张抠图
# 结果首图/机器图全用了开门图）。
_WIDE_CATS = ("油烟", "灶", "电视", "微波", "烤箱", "空调")


def _bbox_ar(f):
    """抠图内容（alpha 非透明区）宽高比；量不出返回 None。
    商城相册图全是 2000×2000 方画布，画布 ar 恒为 1，必须量内容 bbox。"""
    try:
        rgba = Image.open(f).convert("RGBA")
        bb = rgba.getchannel("A").getbbox()
        if not bb:
            return None
        w, h = bb[2] - bb[0], bb[3] - bb[1]
        if w < 220 or h < 220:
            return None
        return w / h
    except Exception:
        return None


def _iter_src_images(p):
    """素材目录原图清单（2026-10-24 A 方案）：根目录 + 官网/欣盛/其他 子文件夹。
    返回 [(Path, rel)]；rel = 相对 src_dir 的路径（根目录文件就是文件名），用于来源分层。"""
    out = []
    for f in sorted(p.iterdir()):
        if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            out.append((f, f.name))
    for sub in ("官网", "欣盛", "其他"):
        sd = p / sub
        if sd.is_dir():
            for f in sorted(sd.iterdir()):
                if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    out.append((f, sub + "/" + f.name))
    return out


def pick_images(src_dir, cat=None):
    p = Path(src_dir)
    imgs_all = _iter_src_images(p)
    if not imgs_all:
        return None, None, None, None
    imgs = [f for f, _r in imgs_all]
    rel_of = dict(imgs_all)      # Path -> rel
    tier = _src_tiers(src_dir)   # 官网 0 > 欣盛 1 > 其他 2：同层内才比 ar

    def find_cut(*keys):
        for f in sorted(imgs, key=lambda x: (tier(rel_of[x]), rel_of[x])):
            n = f.stem.lower()
            if any(k in n for k in keys) and _is_cutout(f) and not _is_junk_cut(f):
                return f
        return None

    def other_cut(exclude):
        for f in sorted(imgs, key=lambda x: (tier(rel_of[x]), rel_of[x])):
            if f not in exclude and _is_cutout(f) and not _is_junk_cut(f):
                return f
        return None

    cuts = []
    for f in imgs:
        if _is_cutout(f) and not _is_junk_cut(f):
            ar = _bbox_ar(f)
            if ar:
                cuts.append((f, ar, tier(rel_of[f])))

    wide = any(w in str(cat or "") for w in _WIDE_CATS)
    tgt = 1.4 if wide else 0.65
    # 正面 = 关门正面：bbox ar 最贴近品类目标的那张抠图。
    # 立式关门带 ≤0.92（开门/特写 ≥0.98 分出去）；找不到合格关门图才走老兜底。
    # 2026-10-24 起同层内比 ar，不同层先比来源（官网 > 欣盛 > 其他）
    closed = [t for t in cuts if t[1] <= (2.0 if wide else 0.92)]
    front = None
    if closed:
        front = min(closed, key=lambda t: (t[2], abs(t[1] - tgt)))[0]
    if front is None:
        for f in sorted(imgs, key=lambda x: (tier(rel_of[x]), rel_of[x])):
            if _is_cutout(f) and not _is_junk_cut(f):
                front = f
                break
        if front is None:
            front = imgs[0]

    # 角度：除正面外另一张关门/斜角（比正面略宽的优先）。
    # 老关键词("07"/角度)碰上编号命名的特写图会翻车（28E 顶板特写 07 实锤），只在排序没得选时用
    rest = [t for t in closed if t[0] != front]
    angle = None
    if rest:
        angle = min(rest, key=lambda t: (t[2], abs(t[1] - (tgt + (0.1 if not wide else 0.2)))))[0]
    if angle is None:
        angle = find_cut("45", "角度", "angle", "侧面")
        if angle is None or angle == front:
            angle = other_cut({front}) or front
    if angle == front:
        angle = other_cut({front}) or front

    # 开门/内部：关键词优先；没有就用「开门特征 ar」（立式 ≥0.98，门甩出来/内部
    # 特写的 bbox 都在这个带里）；再没有回退正面（绝不留白、绝不带底图）
    openv = find_cut("09", "开门", "内部", "open", "冷藏")
    if openv is None or openv in (front, angle):
        if not wide:
            opened = [t for t in cuts if t[0] not in (front, angle) and t[1] >= 0.98]
            if opened:
                openv = min(opened, key=lambda t: (t[2], abs(t[1] - 1.2)))[0]
    if openv is None or openv in (front, angle):
        openv = front

    # 场景：关键词命中的实景照（带底也行），否则 None（img6 回退产品）。
    # 注意：不要用裸编号 "16" 当场景关键词——同编号在不同机型含义不同
    # （洗衣机 16.jpg 是内部结构渲染图，冰箱 16.jpg 才是厨房实景），离线无法分辨，
    # 真正按内容挑实景照交给「AI 视觉选图」(generate 内 zhipu_pick_images)。
    scene = None
    for f in imgs:
        n = f.stem.lower()
        if any(k in n for k in ("厨房", "阳台", "卫生间", "场景", "scene", "实景", "安装")) and _is_photo(f):
            scene = f
            break

    return front, angle, openv, scene


# ================= 文案构建 =================
# 2026-09-15 用户定稿：
#   ① 同一个参数（如 500L / 一级能效）**在九张里只出现一次** —— 每张图各管一摊；
#   ② 其余位置不再复读参数，改用「软卖点」（内嵌 AI 自由写，离线时用下面这套品类兜底）；
#   ③ 软文案必须按品类走，不能给燃气灶写「风冷无霜」。

_KIND_DEFAULT = dict(
    tag="官方正品 · 全国联保", angle="安装省心",
    o=("内部细节 · 用料扎实", "结构看得见 · 买得更放心"),
    scene=("实拍场景", "融入家居 · 安装更省心"),
    energy=("节能省电 · 低噪运行", "长期使用更划算"),
    head={"o": ("空间更大", "一次装得下"), "r": ("融入家居", "安装更省空间"),
          "e": ("节能省电", "长期用更划算"), "d": ("精工选材", "耐磨更耐用")})

_KIND = {
    "冰箱": dict(tag="风冷无霜 · 变频压缩机", angle="纤薄嵌入",
                 o=("开门大容量 · 分区储鲜", "双门全开 · 一周食材一次装下"),
                 scene=("厨房实拍", "纤薄机身 · 嵌入橱柜不突兀"),
                 energy=("一级能效 · 低噪运行", "变频压缩机 · 节能省电"),
                 head={"o": ("分区储鲜", "生熟分开更安心"), "r": ("嵌入橱柜", "齐平不突兀"),
                       "e": ("恒温保鲜", "温度波动更小"), "d": ("精工面板", "耐磨易清洁")}),
    "冷柜": dict(tag="深冷锁鲜 · 低耗运行", angle="卧式省地",
                 o=("深冷大容量 · 囤货无忧", "一次囤够 · 食材久放不串味"),
                 scene=("实拍场景", "卧式小巧 · 小空间也放得下"),
                 energy=("低耗运行 · 安静省电", "环保内胆 · 清洁更省心"),
                 head={"o": ("深冷锁鲜", "食材久放不串味"), "r": ("卧式省地", "小空间也放得下"),
                       "e": ("低耗运行", "长期用更划算"), "d": ("环保内胆", "清洁更省心")}),
    "洗衣机": dict(tag="直驱变频 · 智能投放", angle="超薄嵌入",
                   o=("大筒大容量 · 一次洗更多", "床单被罩 · 一次全搞定"),
                   scene=("阳台实拍", "超薄机身 · 嵌入阳台不占地"),
                   energy=("直驱静音 · 节能省水", "变频电机 · 夜晚洗衣不打扰"),
                   head={"o": ("大筒装得多", "床单被罩一次洗"), "r": ("超薄嵌入", "阳台也能放下"),
                         "e": ("直驱静音", "夜晚不扰眠"), "d": ("筒自洁", "内筒干净更放心")}),
    "干衣机": dict(tag="热泵低温柔烘 · 高效除菌", angle="超薄嵌入",
                   o=("大容量内筒 · 全家衣物一次烘", "衣物不缠绕 · 烘干更均匀"),
                   scene=("阳台实拍", "叠放嵌入 · 省下一半空间"),
                   energy=("热泵省电 · 低温不伤衣", "静音运行 · 夜晚也能烘"),
                   head={"o": ("大容量", "全家衣物一次烘"), "r": ("叠放嵌入", "省下一半空间"),
                         "e": ("热泵省电", "低温更护衣"), "d": ("除菌烘干", "穿着更放心")}),
    "油烟机": dict(tag="大吸力 · 免拆洗自清洁", angle="嵌入安装",
                   o=("深腔拢烟 · 爆炒不跑烟", "大风量瞬吸 · 厨房不见油烟"),
                   scene=("厨房实拍", "嵌入橱柜 · 厨房更整体"),
                   energy=("一级能效 · 低噪运行", "做饭也不吵 · 长开更省电"),
                   head={"o": ("深腔拢烟", "爆炒油烟不扑脸"), "r": ("嵌入橱柜", "厨房更整体"),
                         "e": ("低噪运行", "做饭也不吵"), "d": ("免拆洗", "内腔不用手擦")}),
    "燃气灶": dict(tag="纯蓝猛火 · 熄火保护", angle="台嵌两用",
                   o=("纯蓝猛火 · 燃烧更充分", "大火爆炒 · 小火慢炖都拿手"),
                   scene=("厨房实拍", "台嵌两用 · 装进台面更好看"),
                   energy=("高热效率 · 省气耐用", "火力足更省气 · 用着更安心"),
                   head={"o": ("纯蓝猛火", "爆炒更有锅气"), "r": ("台嵌两用", "装进台面更好看"),
                         "e": ("省气耐用", "火力足更省气"), "d": ("熄火保护", "用着更安心")}),
    "集成灶": dict(tag="烟灶一体 · 大吸力猛火", angle="一体嵌入",
                   o=("烟灶一体 · 占地更少", "下排吸油烟 · 不碰头不跑烟"),
                   scene=("厨房实拍", "一体嵌入 · 台面更整洁"),
                   energy=("高效省气 · 低噪运行", "吸力猛火力足 · 更省心"),
                   head={"o": ("烟灶一体", "占地更少"), "r": ("一体嵌入", "台面更整洁"),
                         "e": ("高效省气", "用着更省心"), "d": ("易清洁", "油污一抹就净")}),
    "热水器": dict(tag="恒温速热 · 多重安全防护", angle="小巧壁挂",
                   o=("恒温出水 · 水温不忽冷忽热", "速热省时 · 不用久等热水"),
                   scene=("安装实拍", "小巧壁挂 · 浴室不占地"),
                   energy=("节能省气 · 低噪运行", "多重防护 · 用气更安心"),
                   head={"o": ("恒温出水", "水温不忽冷忽热"), "r": ("小巧壁挂", "浴室不占地"),
                         "e": ("速热省时", "不用久等热水"), "d": ("多重防护", "用气更安心")}),
    "空调": dict(tag="变频省电 · 静音运行", angle="嵌入安装",
                 o=("循环送风 · 全屋温度更均匀", "快速冷暖 · 回家就舒服"),
                 scene=("家居实拍", "嵌入吊顶 · 出风不直吹"),
                 energy=("一级能效 · 静音运行", "开整晚也安心 · 电费更省"),
                 head={"o": ("快速冷暖", "回家就舒服"), "r": ("出风柔和", "不直吹更舒适"),
                       "e": ("一级省电", "开整晚也安心"), "d": ("自清洁", "出风更干净")}),
}
_KIND_ALIAS = (("油烟机", "油烟机"), ("吸油烟", "油烟机"), ("烟机", "油烟机"),
               ("集成灶", "集成灶"), ("燃气灶", "燃气灶"), ("灶具", "燃气灶"),
               ("热水器", "热水器"), ("干衣机", "干衣机"), ("洗衣机", "洗衣机"),
               ("冷柜", "冷柜"), ("冰箱", "冰箱"), ("空调", "空调"),
               ("洗碗机", "洗碗机"), ("消毒柜", "消毒柜"), ("净水", "净水器"))

# 通用兜底软卖点（补齐每组第 2/3 枚；跨图去重后才会用）
_SOFT_PAD = {
    "o": [("空间更大", "一次装得下"), ("开门易取", "深处也看得见")],
    "r": [("融入家居", "安装更省空间"), ("好打理", "日常清洁省事")],
    "e": [("节能省电", "长期用更划算"), ("运行稳定", "使用更省心")],
    "d": [("多重防护", "使用更安心"), ("密封工艺", "易清洁更卫生")],
}
# 最后兜底（候选全被别的图用掉时才有机会出现）
_SOFT_EXTRA = [("做工扎实", "细节经得起看"), ("用料讲究", "手感更实在"),
               ("安装省心", "售后师傅上门装"), ("经久耐用", "长期用得住"),
               ("细节到位", "整体更有质感"), ("好打理", "擦一擦就干净")]


def _kind(info):
    """细品类：官网 cat 常是「厨电」这种粗分类，所以再从标题+参数里认一次。"""
    txt = (info.get("title") or "") + " " + " ".join(
        str(k) for k, _ in (info.get("props") or [])[:8])
    for kw, name in _KIND_ALIAS:
        if kw in txt:
            return name
    return (info.get("cat") or "").strip() or "家电"


def _kind_cfg(kind):
    return dict(_KIND.get(kind) or _KIND_DEFAULT)


# 布尔型/无信息量的值：不进任何图
_BOOL_VALS = {"支持", "有", "无", "不", "是", "否", "—", "-", "", "不支持", "未知", "已上市"}
# 铭牌/包装/内部件：写上去没有说服力
_SKIP_KEYS = ("净重", "毛重", "包装", "上市时间", "型号", "co含量", "装配软管",
              "上部外形尺寸", "额定压力", "电源线", "插头", "遥控", "附属", "说明书")

# 参数归属：key 命中第一组就归该槽位 —— 保证同一参数不会出现在两张图上
_SLOT_KEYS = (
    ("e", ("能效", "耗电", "24h", "kwh", "w·h", "额定功率", "噪声", "噪音", "声功率")),
    ("o", ("总容积", "容积", "洗涤容量", "脱水容量", "烘干容量", "热水产率",
           "容量", "冷量", "储物")),
    ("r", ("外形尺寸", "台面尺寸", "挖孔尺寸", "尺寸", "厚度", "筒径")),
    ("d", ("材质", "内胆", "工艺", "抗菌", "除菌", "防霉", "自清洁", "燃烧",
           "点火", "熄火", "支锅架", "密封", "降噪")),
    ("m", ("产品类型", "类型", "系列", "造型", "款式", "安装方式", "灶眼", "气源",
           "排烟", "电机", "压缩机", "制冷", "控温", "机身颜色", "颜色", "档速",
           "控制方式")),
)
_CAPS = {"e": 2, "o": 2, "r": 2, "d": 2, "m": 4}   # 每槽位取值上限，超出回流「核心参数」


def _bucket(props):
    """把参数按槽位分桶；同一 key/同一数值不重复，超出上限的回流到核心参数卡。"""
    raw = []
    seen_k = set()
    for k, v in props:
        ks, vs = str(k).strip(), str(v).strip()
        if not ks or not vs or ks in seen_k:
            continue
        if vs in _BOOL_VALS:
            continue
        if any(s in ks.lower() for s in _SKIP_KEYS):
            continue
        seen_k.add(ks)
        raw.append((ks, vs))

    slot = {s: [] for s in _CAPS}
    slot["k"] = []
    for ks, vs in raw:
        low = ks.lower()
        for name, keys in _SLOT_KEYS:
            if any(w in ks or w in low for w in keys):
                # 同槽位内数值去重（如 洗涤容量 10kg / 脱水容量 10kg）
                if any(vs == v2 for _, v2 in slot[name]):
                    break
                slot[name].append((vs, ks))
                break
        else:
            slot["k"].append((vs, ks))

    overflow = []
    for name, cap in _CAPS.items():
        overflow += slot[name][cap:]
        slot[name] = slot[name][:cap]
    slot["k"] = (slot["k"] + overflow)[:6]
    return slot


def build_text(info, version, sell_price, ai_copy=None):
    brand = (info.get("brand") or "海尔").strip()
    ben = {"海尔": "Haier", "卡萨帝": "Casarte", "统帅": "Leader"}.get(brand, brand)
    model = (info.get("model") or info.get("full_model") or "").strip()
    props = info.get("props") or []
    cat = (info.get("cat") or info.get("category") or "冰箱").strip()
    kind = _kind(info)
    kc = _kind_cfg(kind)
    color = (info.get("panel") or info.get("color") or "").strip()
    if not color:
        for k, v in props:
            if "颜色" in str(k):
                color = str(v).strip()
                break
    official = int(info.get("price") or 0)
    shop = int(sell_price) if sell_price else (max(1, int(official * 0.9)) if official else 999)
    shipping = "全国" if version != "B" else "深圳 · 东莞 · 惠州 · 广州"
    dim = (info.get("dimensions") or "").strip()

    slot = _bucket(props)
    m_chips = list(slot["m"])          # 2_机器图  规格（4 枚）
    k_cards = list(slot["k"])          # 4_核心参数（6 张卡）
    o_par = list(slot["o"])            # 5_内部/容量
    r_par = list(slot["r"])            # 6_实景/尺寸
    e_par = list(slot["e"])            # 7_能效/噪音
    d_par = list(slot["d"])            # 8_品质细节

    # ---------- 软卖点：内嵌 AI 写的优先，其次品类头，最后通用兜底 ----------
    ai = ai_copy if isinstance(ai_copy, dict) else {}
    used_soft = set()

    def _ai_pairs(g):
        out = []
        for p in (ai.get(g + "_soft") or []):
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                a, b = str(p[0]).strip(), str(p[1]).strip()
                if a and b:
                    out.append((a, b))
        return out

    def soft(group, n):
        cands = _ai_pairs(group)
        head = (kc.get("head") or {}).get(group)
        if head:
            cands.append(head)
        cands += _SOFT_PAD.get(group, []) + _SOFT_EXTRA
        out = []
        for a, b in cands:
            if len(out) >= n:
                break
            a, b = str(a).strip()[:14], str(b).strip()[:14]
            if not a or a in used_soft:
                continue
            used_soft.add(a)
            out.append((a, b))
        return out

    def chips(params, group, n=3):
        """先放该图专属的硬参数，不足用软卖点补齐（同参数不会再出现在别的图）"""
        ps = [(v, k) for v, k in params[:n]]
        return ps + soft(group, n - len(ps))

    def ai_or(key, fallback):
        v = ai.get(key)
        return str(v).strip() if isinstance(v, str) and v.strip() else fallback

    o_title = ai_or("o_title", kc["o"][0])
    o_sub = ai_or("o_sub", kc["o"][1])
    r_title = ai_or("r_title", kc["scene"][0])
    r_sub = ai_or("r_sub", kc["scene"][1])
    e_title = ai_or("e_title", kc["energy"][0])
    e_sub = ai_or("e_sub", kc["energy"][1])
    tag = ai_or("tag", kc["tag"])

    pl1 = ("产品尺寸 " + dim) if dim else (
        "%s %s" % (k_cards[0][1], k_cards[0][0]) if k_cards else "")
    d_title = ai_or("d_title", "品质细节")

    colors = [(70, 190, 255), (60, 205, 140), (255, 200, 70),
              (90, 205, 255), (170, 135, 255), (120, 215, 255)]
    # 先算 8_品质细节（它最吃「d」组软卖点），再算 4_核心参数（缺位时用剩余软卖点补）
    d_feats = [(v, k, colors[i]) for i, (v, k) in enumerate(chips(d_par, "d", 3))]
    cards = [(v, k, colors[i % len(colors)])
             for i, (v, k) in enumerate(chips(k_cards, "d", 6))]

    return dict(
        brand=brand, ben=ben, model=model, cat=cat, color=color, kind=kind, tag=tag,
        official=official, shop=shop, shipping=shipping,
        specs=m_chips if m_chips else chips([], "m", 4),
        cards=cards,
        angle_title=ai_or("angle_title", "%s · 45°立体" % kind),
        angle_sub=ai_or("angle_sub", (dim + " · " + kc["angle"]) if dim else kc["angle"]),
        dim1=pl1, dim2=("%s · 全新未拆封" % tag), pl1=pl1, pl2=tag,
        o_title=o_title, o_sub=o_sub, o_chips=chips(o_par, "o", 3),
        o_foot="%s %s · 全新未拆封" % (brand, model),
        r_title=r_title, r_sub=r_sub, r_chips=chips(r_par, "r", 3),
        r_foot="%s %s · 全新未拆封" % (brand, model),
        e_title=e_title, e_sub=e_sub, e_chips=chips(e_par, "e", 3),
        e_foot="%s %s · 全新未拆封" % (brand, model),
        d_title=d_title, d_feats=d_feats, d_foot="%s · 全新未拆封" % tag,
    )


# ================= 主入口 =================
def generate(info, src_dir, out_dir, version="N", sell_price=None, bg_dir=None,
             api_key=None, progress=None, ai_copy=None, pop=False, slots=None,
             style=None, overview=True, tune=None):
    """生成套图（默认 9 张 + 九宫格总览；slots 指定则只出对应几张，overview=False 不拼总览），
    返回路径列表。

    风格 style（4 选 1，互不影响、各自独立背景缓存）：
      · studio 深灰影棚（默认）：深灰冷灰底，白字
      · spot   暗角聚光：近黑底+中心聚光，机器放大，独立风格背景（修过「显眼版复用普通版底图」的 bug）
      · white  亮白影棚：浅灰→白亮底，深色字
      · scene  实景合成：按品类用 AI 生成真实家居场景（dark 保证白字可读），机器合成进去

    背景 = app 内嵌 AI（CogView）实时出图，按机型+风格缓存到 素材/<model>/AI背景/；
    api_key 即 app 的 ZHIPU_KEY。文案软卖点优先让内嵌 AI 自由发挥，参数一律取自 info。

    progress: 可选回调 progress(百分比, 说明文字)。
    """
    global P, BG_DIR, API_KEY, _TONE, BG_SEED, GEN_POP, BAND_MAXW, POP_FLAVOR, \
        GEN_STYLE, GEN_CAT, TX1, TX2, TX3, TX_GOLD, SCRIM_COL, SCRIM_A, TUNE

    def step(p, m):
        if progress:
            try:
                progress(p, m)
            except Exception:
                pass

    # 自动优化微调量：传入 tune 则应用，否则恢复中性默认（确保正常出图不被上次优化污染）
    TUNE = dict(tune) if tune else {"machine_scale": 1.0, "top_pad": 0, "font_scale": 1.0}

    # 风格解析：兼容旧 pop 参数；style 决定背景生成器 + 是否暗角聚光 + 文字配色
    GEN_STYLE = style or ("spot" if pop else "studio")
    GEN_CAT = (info.get("cat") or "")
    GEN_POP = (GEN_STYLE == "spot")
    BAND_MAXW = 1040 if GEN_POP else None
    POP_FLAVOR = _pop_flavor(info) if GEN_POP else None
    # 风格配色：亮白风用深色字 + 浅色压暗条；其余用白字 + 深色压暗条
    if GEN_STYLE in ("white", "pure"):
        TX1, TX2, TX3 = "#1a1d22", "#3a4250", "#5a6573"
        SCRIM_COL, SCRIM_A = (250, 250, 252), 170
    else:
        TX1, TX2, TX3 = "#ffffff", "#cfd8e4", "#aeb4bd"
        SCRIM_COL, SCRIM_A = (8, 10, 14), 205

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    # 1) 内嵌 AI 写软卖点（九张图不再复读参数）
    if ai_copy is None and api_key and core is not None:
        step(3, "正在让 AI 视觉读懂产品 ...")
        try:
            ai_copy = core.zhipu_product_read(api_key, src_dir, info)
        except Exception:
            ai_copy = None
        if ai_copy is None:
            step(3, "视觉读取失败，改用文本 AI 写卖点 ...")
            try:
                ai_copy = core.zhipu_ai9_copy(api_key, info, version)
            except Exception:
                ai_copy = None
    P = build_text(info, version, sell_price, ai_copy)
    # 机型专属背景微差：每台机器的深灰影棚各不相同（同冷灰族）
    BG_SEED = _seed_phrase(info)

    step(12, "正在挑选素材图 ...")
    BG_DIR = bg_dir or (Path(src_dir) / "AI背景")
    API_KEY = api_key
    front_f, angle_f, open_f, scene_f = pick_images(src_dir, cat=GEN_CAT)
    if not front_f:
        raise RuntimeError("素材目录没有可用的图片：%s" % src_dir)

    # AI 视觉选图（有 key 时）：按内容挑真实实景照、纠正规则选图误判
    # （如洗衣机 16.jpg 实为内部结构渲染图，规则选图误当场景；视觉模型能识别并排除）
    ai_pick = None
    if api_key and core is not None:
        try:
            ai_pick = core.zhipu_pick_images(api_key, src_dir, P.get("kind"))
        except Exception:
            ai_pick = None
    if ai_pick:
        def _vc(fn):
            fp = Path(src_dir) / fn
            return fp if (fp.exists() and _is_cutout(fp) and not _is_junk_cut(fp)) else None
        def _vs(fn):
            fp = Path(src_dir) / fn
            return fp if (fp.exists() and _is_photo(fp)) else None
        if ai_pick.get("front"):
            front_f = _vc(ai_pick["front"]) or front_f
        if ai_pick.get("angle"):
            angle_f = _vc(ai_pick["angle"]) or angle_f
        if ai_pick.get("open"):
            open_f = _vc(ai_pick["open"]) or open_f
        if ai_pick.get("scene"):
            scene_f = _vs(ai_pick["scene"])
        # 场景页标题随 AI 认出的真实场景走（阳台实拍 / 厨房实拍 / ...）
        if ai_pick.get("scene_label"):
            P["r_title"] = ai_pick["scene_label"]
            if ai_pick.get("scene_sub"):
                P["r_sub"] = ai_pick["scene_sub"]

    # 无真实实景照：回退「产品摆影棚」，标题不可用「阳台/厨房实拍」误导，改用中性「产品实拍」
    if not scene_f:
        P["r_title"] = "产品实拍"
        P["r_sub"] = "全新未拆封 · 官方正品"

    front = load_cut(front_f)
    # 产品主色 -> 传给内嵌 AI 出背景做同色系微调（每台机器略有差异但都在深灰族）
    if core_hero is not None:
        try:
            _TONE = core_hero.product_tone(front)
        except Exception:
            _TONE = None
    angle = load_cut(angle_f) if angle_f else front
    openv = load_cut(open_f) if open_f else front
    scene = load_cut(scene_f) if scene_f else None

    # 2) 先把两套背景（亮/暗）出好，后面的 9 张就都是本地合成，进度条才准
    step(16, "正在让 AI 出背景（按机型）...")
    prep_bg("a")
    prep_bg("char")

    # 9 张按槽位定义；slots=None 出全部 9 张（默认「发布图」），
    # 指定 slots（如 [1,2,3,4,6,9]）则只出对应几张（「生成6张」按钮用）。
    STEPS = [
        (1, "首图", lambda: img1(front, out)),
        (2, "机器图", lambda: img2(front, out)),
        (3, "立体角度", lambda: img3(angle, out)),
        (4, "核心参数", lambda: img4(front, out)),
        (5, "内部容量", lambda: img5(openv, out)),
        (6, "实拍场景", lambda: img6(scene, front, out)),
        (7, "能效静音", lambda: img7(front, out)),
        (8, "品质细节", lambda: img8(front, out)),
        (9, "交易保障", lambda: img9(out)),
    ]
    if slots is None:
        slots = [s for s, _, _ in STEPS]
    sel = [(s, lab, fn) for s, lab, fn in STEPS if s in slots]
    n = len(sel)
    paths = []
    for i, (s, label, fn) in enumerate(sel):
        pct = 26 + int((90 - 26) * i / max(1, n - 1))
        step(pct, "正在合成 %d/%d %s ..." % (i + 1, n, label))
        paths.append(fn())
    if overview:
        step(95, "正在拼总览 ...")
        sheet = contact_sheet(out, paths)
        if sheet is not None:
            paths.append(sheet)
    step(100, "套图已生成")
    return paths


# ================= 自动优化（仅确定性布局类） =================
# 自检(qa_render) 只出报告；这里把报告里的「确定性布局类」问题自动修掉：
#   · text_overlap 文字压住机器/重叠  -> 缩小机器 + 略缩字号，腾出四周安全边距
#   · product_clip  机器被画布裁掉     -> 缩小机器 + 下移顶部安全边距
#   · text_clip     文字被截断/溢出     -> 缩小字号
# 通过 tune 微调后只重渲出问题的槽位(slots)，不动其它张；复检(需 key)确认干净或达轮数上限。
# AI/画质类（style_mismatch 风格不统一 / broken 破图 / text_mismatch 图文不符）标「需人工」，不处理。
_QA_DET = {"text_overlap", "text_clip", "product_clip"}


def _infer_code(issue):
    """模型没回 code 时，从中文 issue 反推确定性布局类代码（否则归为需人工）。"""
    t = issue or ""
    if any(k in t for k in ("压", "重叠", "盖住", "挡住", "压住")):
        return "text_overlap"
    if any(k in t for k in ("裁", "缺边", "缺角", "被切", "切掉", "出画")):
        return "product_clip"
    if any(k in t for k in ("截断", "溢出", "出界", "太长", "放不下")):
        return "text_clip"
    return None


def auto_optimize(api_key, info, src_dir, out_dir, style, issues,
                  rounds=2, progress=None, log=None):
    """issues: qa_render 返回的问题列表（含 slot/issue/severity/code）。
    返回 dict: {"fixed":[槽位], "remaining":[仍需人工的 issue], "tune":{...}, "rounds": n}。
    无 key 时仍按给定 issues 应用一轮保守微调（无法复检）。"""
    def step(p, m):
        if progress:
            try:
                progress(p, m)
            except Exception:
                pass

    def lg(s):
        if log:
            try:
                log(s)
            except Exception:
                pass

    det = []
    for it in (issues or []):
        code = it.get("code") or _infer_code(it.get("issue"))
        if code in _QA_DET and "slot" in it:
            det.append({"slot": it["slot"], "code": code,
                        "issue": it.get("issue"), "severity": it.get("severity")})
    if not det:
        lg("自检无确定性布局类问题，无需自动优化")
        return {"fixed": [], "remaining": [i for i in (issues or [])],
                "tune": {}, "rounds": 0}

    affected = sorted(set(d["slot"] for d in det))
    cur = {"machine_scale": 1.0, "top_pad": 0, "font_scale": 1.0}
    remaining = list(issues or [])
    done = 0
    for r in range(max(1, rounds)):
        done = r + 1
        for d in det:
            sev = d.get("severity")
            f = 0.95 if sev == "高" else (0.97 if sev == "中" else 0.985)
            if d["code"] == "text_overlap":
                cur["machine_scale"] *= f
                cur["font_scale"] *= 0.98
            elif d["code"] == "product_clip":
                cur["machine_scale"] *= (f - 0.02)
                cur["top_pad"] += 10
            elif d["code"] == "text_clip":
                cur["font_scale"] *= f
        step(20 + r * 30, "按自检结果微调重渲 第 %d/%d 轮 ..." % (r + 1, rounds))
        paths = generate(info, src_dir, out_dir, style=style, slots=affected,
                         tune=dict(cur), overview=False, api_key=api_key,
                         progress=progress)
        if not api_key or core is None:
            lg("无 AI key，无法复检；已应用一轮微调")
            # 无 key 无法复检，但确定性类已尝试修复，只把非确定性（需人工）项列为 remaining
            remaining = [x for x in (issues or [])
                         if (x.get("code") or _infer_code(x.get("issue"))) not in _QA_DET]
            break
        new = core.qa_render(api_key, paths, info)
        if not new:                      # 复检无问题
            remaining = []
            break
        det2 = [x for x in new
                if (x.get("code") or _infer_code(x.get("issue"))) in _QA_DET
                and x["slot"] in affected]
        if not det2:                    # 确定性类已清，剩下的是需人工的
            remaining = [x for x in new
                         if (x.get("code") or _infer_code(x.get("issue"))) not in _QA_DET]
            break
        det = det2
        remaining = new
    lg("自动优化完成：修 %s，余 %d 项需人工" % (affected, len(remaining)))
    return {"fixed": affected, "remaining": remaining, "tune": cur, "rounds": done}


if __name__ == "__main__":
    import sys, json
    # 简易自测：传入素材目录与 info.json
    if len(sys.argv) < 3:
        print("usage: python core_ai9.py <src_dir> <out_dir> [info.json]")
        sys.exit(1)
    sd = sys.argv[1]; od = sys.argv[2]
    info = {}
    if len(sys.argv) >= 4 and os.path.exists(sys.argv[3]):
        info = json.load(open(sys.argv[3], encoding="utf-8"))
    ps = generate(info, sd, od)
    print("生成 %d 张 -> %s" % (len(ps), od))
    for p in ps:
        print("  ", Path(p).name)
