# -*- coding: utf-8 -*-
"""闲鱼配文 · 核心模块：官网抓取 + 文案生成 + 拼版主图"""
import os, re, json, ssl, gzip, time, shutil, zipfile, threading, sys, io, base64, random, hashlib
from pathlib import Path
import urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont
from net import urlopen_ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"

HOSTS = {"haier": "https://www.haier.com", "casarte": "https://www.casarte.com",
         "leader": "https://www.leader.com.cn", "xdjdy": "https://www.xdjdy.com"}
IMGHOSTS = {"haier": "https://image.haier.com", "casarte": "https://image.casarte.com",
            "leader": "https://image.leader.com.cn"}
BRAND_CN = {"haier": "海尔", "casarte": "卡萨帝", "leader": "统帅", "xdjdy": "兴达商城"}

CAT_CN = {"cooling": "冰箱", "freezer": "冷柜", "laundry": "洗衣机", "xjd": "洗衣机",
          "jsj": "净水器",  # 净水机挂在二级段 /xjd/jsj/，深段优先（2026-10-22 HKC3000 实锤）
          "air_conditioners": "空调", "central-air-conditioning": "中央空调",
          "water-heater": "热水器", "water_heaters": "热水器", "televisions": "电视",
          "tv": "电视", "kitchen_appliances": "厨电", "kitchen-appliances": "厨电",
          "ice-bar": "冰吧", "ice_bar": "冰吧", "wine": "酒柜", "computers": "电脑"}

BLOCK_N = "海尔官方正品，全新机，全国联保\n支持专业送货安装，售后无忧\n可发货地区：全国"
BLOCK_B = "海尔官方正品，全新机，全国联保\n支持专业送货安装，售后有保障\n可发货地区：深圳、东莞、惠州、广州"

_FONTS = ["C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
          "C:/Windows/Fonts/simhei.ttf",
          "/System/Library/Fonts/PingFang.ttc",
          "/System/Library/Fonts/Supplemental/Songti.ttc"]


def font(size, bold=True):
    """跨平台中文字体：Windows 微软雅黑 / macOS 苹方。
    ⚠️ bold=True 必须优先加载**真正的粗体文件**（msyhbd / 苹方 Semibold）。
    旧写法把 bold 参数忽略了、永远只加载常规体，粗体全靠 stroke 描边冒充，
    结果字形发糊、和正规粗体完全不是一个东西。"""
    if bold:
        cands = [("C:/Windows/Fonts/msyhbd.ttc", 0),          # 微软雅黑 Bold
                 ("/System/Library/Fonts/PingFang.ttc", 6),   # 苹方 Semibold
                 ("C:/Windows/Fonts/msyh.ttc", 0),
                 ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
                 ("/System/Library/Fonts/Supplemental/Songti.ttc", 1)]
    else:
        cands = [("C:/Windows/Fonts/msyh.ttc", 0),
                 ("/System/Library/Fonts/PingFang.ttc", 0),
                 ("/System/Library/Fonts/STHeiti Light.ttc", 0),
                 ("/System/Library/Fonts/Supplemental/Songti.ttc", 0)]
    for p, idx in cands:
        if not os.path.exists(p):
            continue
        try:
            f = ImageFont.truetype(p, size, index=idx)
        except Exception:
            continue
        if _font_has_cjk(f):
            return f
    return ImageFont.load_default()


# 衬线（宋体）字体：首图价格标专用，与黑体主字体区分开
# ⚠️ 千万别放 simsunb.ttf —— 它是 SimSun-**ExtB**，只含扩展 B 区生僻字，
#    常用汉字和 ¥ 全都缺，渲染出来是一排「豆腐块」方框。
_SERIF_FONTS = ["C:/Windows/Fonts/STZHONGS.TTF",       # 华文中宋（宋体家族，偏粗）
                "C:/Windows/Fonts/simsun.ttc",         # 宋体
                "/System/Library/Fonts/Supplemental/Songti.ttc",
                "/System/Library/Fonts/STSong.ttf"]


def _font_has_cjk(f):
    """自检字体是否真含常用汉字：若「官」「新」「全」渲染结果完全相同，
    说明它们都是同一个豆腐块 -> 该字体缺中文字形，不能用。"""
    try:
        def _r(ch):
            im = Image.new("L", (48, 48), 0)
            ImageDraw.Draw(im).text((4, 4), ch, font=f, fill=255)
            return im.tobytes()
        return not (_r("官") == _r("新") == _r("全"))
    except Exception:
        return False


def font_serif(size):
    """宋体/衬线字体（价格标那种金底金字用它，别用黑体）。
    逐个候选做字形自检，避免又选到只有生僻字的 ExtB 字体。"""
    for p in _SERIF_FONTS:
        if not os.path.exists(p):
            continue
        try:
            f = ImageFont.truetype(p, size)
        except Exception:
            continue
        if _font_has_cjk(f):
            return f
    return font(size)


def load_rgb_img(img):
    """内存图像转 RGB；透明 PNG/La/P 先在白底上合成 —— 直接 convert("RGB")
    会把透明区变黑底（闲鱼产品图白底变黑底的元凶）"""
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        return bg
    return img.convert("RGB")


def load_rgb(path):
    return load_rgb_img(Image.open(path))


def res_path(name):
    """PyInstaller 资源路径：frozen 时取 _MEIPASS，否则取本文件目录。
    2026-10-17 并入型号查询：索引/URL 种子统一放 assets/pei/ 子目录（开发态与 _MEIPASS 同构）。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(base, name)
    if os.path.exists(p):
        return p
    return os.path.join(base, "assets", "pei", name)


def _referer_for(url):
    """三站图片 CDN 都有防盗链（不带 Referer 一律 403），且统帅严格要求
    Referer 必须是 leader.com.cn，带 haier.com 也会被拒。按域名选对应 Referer。"""
    h = (url or "").lower()
    if "leader" in h:
        return "https://www.leader.com.cn/"
    if "casarte" in h:
        return "https://www.casarte.com/"
    return "https://www.haier.com/"


def http(url, n=None, tries=3):
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": _referer_for(url)})
            with urlopen_ssl(req, timeout=40) as r:
                d = r.read(n) if n else r.read()
            if d[:2] == b"\x1f\x8b":
                try:
                    d = gzip.decompress(d)
                except Exception:
                    pass
            return d
        except Exception:
            time.sleep(0.5)
    # 2026-11-10：海尔系官网/图床对本机出口 IP 段封禁（leader 全站 403、image.haier.com
    # TCP RST，换 UA/真浏览器/机场节点全无解）。常规路走不通就对海尔系域名换 CF 出口
    # 代抓（worker /osfetch，实测 200）——fetch_product 的页面和图片下载都从这里过，
    # 一处兜底全链路吃到。
    try:
        from net import PX_HOST_RE, relay_fetch
        if PX_HOST_RE.match(url or ""):
            d = relay_fetch(url, binary=True, timeout=45)
            if n:
                d = d[:n]
            if d[:2] == b"\x1f\x8b":
                try:
                    d = gzip.decompress(d)
                except Exception:
                    pass
            return d if d else None
    except Exception:
        pass
    return None


def get_text(url, n=None):
    d = http(url, n)
    if not d:
        return None
    for enc in ("utf-8", "gbk"):
        try:
            return d.decode(enc)
        except Exception:
            continue
    return d.decode("utf-8", "ignore")


# ---------------- 型号定位 ----------------
_idx_cache = {}
_idx_lock = threading.Lock()


def _site_index_path(site):
    """站点专属索引 JSON 路径"""
    if site == "xdjdy":
        return res_path("xdjdy_index.json")
    return res_path("%s_index.json" % site)


def load_index(site):
    with _idx_lock:
        if site in _idx_cache:
            return _idx_cache[site]
        p = _site_index_path(site)
        d = {}
        if p and os.path.exists(p):
            try:
                d = json.load(open(p, encoding="utf-8"))
            except Exception:
                d = {}
        # 即使没找到索引文件，也返回空 dict 让后续走 sitemap 扫描
        _idx_cache[site] = d
        return d


def _normalize_key(s):
    """统一型号匹配键：去空格、转大写，保留中文/括号等原始修饰"""
    return str(s).upper().replace(" ", "")


def lcs_length(a, b):
    """最长公共子串长度（匹配的字最多的）"""
    if not a or not b:
        return 0
    m, n = len(a), len(b)
    prev = [0] * (n + 1)
    best = 0
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        ai = a[i - 1]
        for j in range(1, n + 1):
            if ai == b[j - 1]:
                val = prev[j - 1] + 1
                curr[j] = val
                if val > best:
                    best = val
        prev = curr
    return best


def match_score(query, candidate):
    """匹配分数：完全相等 > 包含关系 > 最长公共子串

    注意：包含关系要求「短的那边 >= MIN_CONTAIN 个字符」。否则像候选 "CS"
    这种两三字符的碎片，只要恰好是查询串的子串（如 BCD-701WL**CS**SKAHU1）
    就会被误判成包含关系拿到 5000 分，造成严重误命中。
    """
    q = _normalize_key(query)
    c = _normalize_key(candidate)
    if not q or not c:
        return 0
    if q == c:
        return 10000
    lo, hi = (q, c) if len(q) <= len(c) else (c, q)
    if len(lo) >= MIN_CONTAIN and lo in hi:
        return 5000 + lcs_length(q, c)
    return lcs_length(q, c)


# 判定「包含关系」的最短长度：短于这个值的碎片（如 "CS"）不算包含，只按公共子串算
MIN_CONTAIN = 5


def best_match(query, candidates):
    """从候选中选出与 query 匹配度最高的，返回 (candidate, score)"""
    best_c, best_s = None, -1
    for c in candidates:
        s = match_score(query, c)
        if s > best_s:
            best_s, best_c = s, c
    return best_c, best_s


# 型号提取正则：允许尾接颜色+可选括号数字，如 LES60H-P1金(1)
_MODEL_RE = re.compile(
    r"[A-Za-z]{2,4}-?\d{1,4}[A-Za-z0-9\-]*"
    r"(?:[金银白黑红蓝灰](?:\(\d+\))?|\(\d+\))?"
)


def clean_model(model):
    """提取型号主段（保留颜色/括号后缀）。例：'LES60H-P1金(1)新' -> 'LES60H-P1金(1)'"""
    s = str(model).strip()
    cands = _MODEL_RE.findall(s)
    if cands:
        return max(cands, key=len).upper()
    return _normalize_key(s)


def _model_scores(text, target_key):
    """在文本中提取可能的型号并打分，返回 (最佳型号, 分数)
    核心：候选与 target_key 的最长公共子串越长，分数越高
    """
    scores = {}
    # 1. HTML 属性/标签中的 sku/model（可能含中文/括号）
    for m in re.finditer(
        r'(?:sku|model|productModel|goodsCode|itemCode)["\s]*[:=]["\s]*([^"\s>]+)',
        text, re.I
    ):
        cand = m.group(1).strip('"\'')
        s = match_score(target_key, cand)
        if s > 0:
            scores[cand] = max(scores.get(cand, 0), s + 50)

    # 2. JSON LD 数据中的 sku/model
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', text, re.S):
        try:
            d = json.loads(m.group(1).strip())
            g = d.get("@graph") if isinstance(d, dict) else d
            nodes = g if isinstance(g, list) else [d]
            for n in nodes:
                if isinstance(n, dict) and n.get("@type") == "Product":
                    for field in ["sku", "model", "productModel", "goodsCode", "itemCode"]:
                        if field in n:
                            cand = n[field]
                            s = match_score(target_key, cand)
                            if s > 0:
                                scores[cand] = max(scores.get(cand, 0), s + 40)
        except Exception:
            continue

    # 3. 页面文本中的型号片段
    for m in re.finditer(_MODEL_RE, text):
        cand = m.group(0)
        s = match_score(target_key, cand)
        if s > 0:
            scores[cand] = max(scores.get(cand, 0), s)

    if scores:
        best = max(scores.items(), key=lambda x: x[1])
        return best[0], best[1]
    return None, 0


def _sitemap_urls(site):
    host = HOSTS[site]
    sm = get_text(host + "/sitemap.xml", 4_000_000)
    if not sm:
        return []
    locs = re.findall(r"<loc>([^<]+)</loc>", sm)
    if len(locs) < 10:
        sub = []
        for s in locs[:8]:
            t = get_text(s, 4_000_000)
            if t:
                sub += re.findall(r"<loc>([^<]+)</loc>", t)
        locs = sub
    return locs


def _is_strong(score, key, cand=None):
    """判定一次匹配是否足够可信，避免在大索引里误命中无关型号。
    - 完全相等 / 包含关系 (score>=5000)：可信
    - 2026-10-23 用户定稿「型号对不上的就不要了」：LCS 公共子串一律不算强命中——
      XQG100-28E 与统帅 LHD559WU1 的 LCS=7，全靠系列前缀「XQG100-」撑着，
      旧阈值(≥70%覆盖)把人家白机页误当强命中，素材/发布图全成了别家型号（实锤）。
      仅放宽「连字符写法差异」（XQG10028E = XQG100-28E）。"""
    if score >= 5000:
        return True
    if cand:
        _nd = re.sub(r"[-\-－—]+", "", _normalize_key(cand))
        _nk = re.sub(r"[-\-－—]+", "", _normalize_key(key))
        if _nk and _nk == _nd:
            return True
    return False


def find_page(model, sites=("haier", "casarte", "leader"), log=None):
    # 保留原始修饰（金/新/(1) 等），只去空格、转大写
    key = _normalize_key(model)
    # 阶段一：本地索引匹配（仅接受 完全相等/包含关系，拒绝大索引里的巧合子串误命中）
    for site in sites:
        if log:
            log("查 %s 本地型号库 ..." % BRAND_CN[site])
        # xdjdy（渠道商城）：本地索引匹配 -> 在线搜索兜底（find_xdjdy 在 pei_xdjdy.py，
        # 2026-10-19 修复：原 import core_xdjdy 是改名前残留，装版走到这就炸 No module named）
        if site == "xdjdy":
            import pei_xdjdy as core_xdjdy
            u = core_xdjdy.find_xdjdy(model, log=log)
            if u:
                return "xdjdy", u
            continue
        idx = load_index(site)
        if not idx:
            continue
        best_key, best_score = best_match(key, [k for k in idx if len(k) >= 3])
        if best_key and best_score >= 5000:
            if log:
                log("  索引命中：%s (score=%d)" % (best_key, best_score))
            return site, idx[best_key]["url"]
    # 阶段二：扫描官网产品页，按 LCS 找匹配度最高的型号
    for site in sites:
        if site == "xdjdy":
            continue  # 渠道商城只在阶段一处理
        if log:
            log("扫描 %s 官网产品页 ..." % BRAND_CN[site])
        urlfile = res_path("site_urls_%s.txt" % site)
        # 2026-11-04 P1-5：0 字节索引文件（site_urls_haier.txt 实锤）也会挡住 sitemap 兜底——
        # 只有「存在且非空」才直接用，否则一律走 sitemap 现抓
        urls = []
        if os.path.exists(urlfile):
            urls = [u.strip() for u in open(urlfile, encoding="utf-8") if u.strip()]
        if not urls:
            urls = [u for u in _sitemap_urls(site) if re.search(r"/\d{8}_\d+\.shtml", u)]
            try:
                open(urlfile, "w", encoding="utf-8").write("\n".join(urls))
            except Exception:
                pass
        found = []
        lock = threading.Lock()

        def w(u):
            h = get_text(u, 24576)
            if not h:
                return
            matched_model, score = _model_scores(h, key)
            # 用原始 match_score（不含 _model_scores 的 bonus）判断可信度，避免
            # 无关页面里某个 HTML 属性值因 +50 bonus 被误判为强匹配
            if matched_model and _is_strong(match_score(key, matched_model), key, matched_model):
                with lock:
                    found.append((u, matched_model, score))

        with ThreadPoolExecutor(max_workers=16) as ex:
            list(ex.map(w, urls))
        if found:
            # 按分数排序，高分优先；同分优先匹配字更多（型号更长=包含更多匹配字符）
            found.sort(key=lambda x: (x[2], len(x[1])), reverse=True)
            if log:
                log("  命中 %d 个页面，最高分: %s (score=%d)" % (len(found), found[0][0], found[0][2]))
            return site, found[0][0]
    return None, None


# ---------------- 产品解析 ----------------
def parse_ld(h):
    for m in re.finditer(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", h, re.S):
        try:
            d = json.loads(m.group(1).strip())
        except Exception:
            continue
        g = d.get("@graph") if isinstance(d, dict) else d
        nodes = g if isinstance(g, list) else [d]
        for n in nodes:
            if isinstance(n, dict) and n.get("@type") == "Product":
                return n
    return {}


def collect_images(h, site, cat):
    """收集页面里所有产品图：常规 <img> + CSS background-image，含尺寸图/规格图。
    不再过滤 logo/图标/尺寸图等，全部下载，由用户自行挑选发布。"""
    imghost = IMGHOSTS[site]
    urls = []
    # 1) 常规 <img src/data-src/data-original> 与 meta content
    pat = re.compile(
        r'(?:src|data-src|data-original|content)="'
        r'((?:https?:)?//[^"]+\.(?:jpg|jpeg|png|webp)|(?:W\d{20,}\.(?:jpg|jpeg|png|webp)))"',
        re.I)
    for m in pat.finditer(h):
        urls.append(m.group(1))
    # 2) CSS 背景图：尺寸图/规格图常以内联 style 形式存在
    pat2 = re.compile(
        r'url\(\s*["\']?((?:https?:)?//[^"\')]+\.(?:jpg|jpeg|png|webp)|'
        r'/?[^"\')]*\.(?:jpg|jpeg|png|webp))["\']?\s*\)', re.I)
    for m in pat2.finditer(h):
        urls.append(m.group(1))
    out, seen = [], set()
    for u in urls:
        u = u.strip()
        if not u:
            continue
        if u.startswith("//"):
            u = "https:" + u
        elif u.startswith("/"):
            u = imghost + u
        elif not u.startswith("http"):
            u = "%s/%s/%s" % (imghost, cat, u)
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def fetch_product(site, url, outdir, log=None, progress=None):
    def cb(p, m):
        if progress:
            progress(p, m)
    cb(0, "正在抓取产品页 ...")
    h = get_text(url)
    if not h:
        cb(100, "抓取失败")
        return None
    path = re.sub(r"[?#].*$", "", url)
    segs = [s for s in path.split("/") if s]
    if segs and "." in segs[-1]:
        segs = segs[:-1]
    catpath = "/".join(segs[2:] if len(segs) > 2 and ":" in url else segs)  # 相对图片路径前缀 'xjd/jsj'
    cat = ""
    for s in reversed(segs):
        if s in CAT_CN:
            cat = s  # 品类取最深匹配段：/xjd/jsj/ 是净水器不是洗衣机（2026-10-22 实锤）
            break
    if not cat and segs:
        cat = segs[0]
    p = parse_ld(h)
    model = p.get("sku", "") or ""
    if not model:
        m = re.search(r'name="keywords"\s+content="([^"]+)"', h)
        model = m.group(1).split(",")[0].strip() if m else "未知型号"
    title = p.get("name", "")
    price = 0
    try:
        price = int(float(p.get("offers", {}).get("price", 0)))
    except Exception:
        pass
    avail = p.get("offers", {}).get("availability", "")
    props = [(a.get("name", ""), str(a.get("value", "")) + (a.get("unitText") or ""))
             for a in (p.get("additionalProperty") or []) if a.get("name")]
    main_img = ""
    m = re.search(r'itemprop="image"\s+content="([^"]+)"', h)
    if m:
        main_img = m.group(1)
    imgs = collect_images(h, site, catpath)
    order = ([main_img] if main_img else []) + [u for u in imgs if u != main_img]
    # 不再截断：尺寸图/规格图等全部下载，用户自行挑选

    os.makedirs(outdir, exist_ok=True)

    cb(15, "开始下载图片 ...")
    def dl(a):
        u, path = a
        d = http(u)
        if d and len(d) > 1024:
            open(path, "wb").write(d)
            return 1
        return 0

    jobs = []
    for i, u in enumerate(order):
        ext = os.path.splitext(u.split("?")[0])[1] or ".jpg"
        jobs.append((u, os.path.join(outdir, "%02d%s" % (i + 1, ext))))
    ok = 0
    n = max(1, len(jobs))
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(dl, jobs):
            ok += r
            cb(15 + 80 * ok // n, "下载图片 %d/%d" % (ok, n))
    if log:
        log("图片下载 %d/%d 张" % (ok, len(jobs)))

    cb(98, "保存素材信息 ...")
    info = {"site": site, "brand": BRAND_CN.get(site, site), "model": model,
            "title": title, "price": price, "avail": avail, "props": props,
            "url": url, "dir": outdir, "cat": CAT_CN.get(cat, guess_cat(title))}
    json.dump(info, open(os.path.join(outdir, "info.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    cb(100, "完成")
    return info


# ---------------- 第一步·新爬虫（official_spider）----------------
# 用官网检索接口（/igs/front/search.jhtml）+ Bing 站内搜索兜底来「定位详情页」，
# 比旧的「扫 site_urls_<site>.txt 全站 url 列表」快得多（实测 1.2s），且能覆盖已下架的型号。
#
# ⚠️ 两条硬约束（2026-09-29 定）：
#   1. **新爬虫不下载图片** —— 图片仍由上面的 fetch_product/collect_images 负责。
#      所以这里只做「定位」和「信息补充」，绝不整段替换 fetch_product。
#   2. **不覆盖已有 props** —— 爬虫把单位写在键里（`总容积(L)`=500），而 App 把单位并进值里
#      （`总容积`=500L），描述模板与 4_核心参数.jpg 都依赖后者。故仅当 props 为空时才用爬虫参数兜底。
#
# 新爬虫只覆盖 haier/casarte/leader；xdjdy（兴达商城）仍走 core_xdjdy。
_SPIDER_SITE_HOSTS = {"haier": "haier.com", "casarte": "casarte.com",
                      "leader": "leader.com.cn"}


def _site_of_url(url):
    u = (url or "").lower()
    for k, h in _SPIDER_SITE_HOSTS.items():
        if h in u:
            return k
    return None


def spider_locate(model, log=None):
    """用 official_spider 定位官网详情页 -> (site, url, prod)；未命中返回 (None, None, None)。

    全程只读；任何异常都吞掉并返回未命中，让调用方安全回退旧路径。
    """
    try:
        import pei_spider as _osp
    except Exception as e:
        if log:
            log("新爬虫不可用（%s），走旧检索" % e)
        return None, None, None
    try:
        prod, _cands = _osp.resolve(model)
    except Exception as e:
        if log:
            log("新爬虫检索异常（%s），走旧检索" % e)
        return None, None, None
    url = (prod or {}).get("source_url") or ""
    site = _site_of_url(url)
    if not (prod and url and site):
        return None, None, None
    if log:
        log("新爬虫命中：%s（%s）" % (url, prod.get("source", "")))
    return site, url, prod


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_UNIT_CHARS = r"[A-Za-z0-9/·.*()（）\-]"


def _split_key_unit(k):
    """把爬虫参数键拆成 (裸键, 单位)：爬虫把单位写在键里，App 惯用写在值里。

        总容积(L)            -> (总容积, L)
        产品尺寸(宽*高*深)mm   -> (产品尺寸, mm)
        噪声dB(A)            -> (噪声, dB(A))
        搁物架(个)            -> (搁物架, 个)
    拆不出来（或单位像「说明文字」而非单位）就返回 (原键, '')，保证不误伤。
    """
    k = str(k or "").strip()
    if not k:
        return k, ""
    # ★ 单位必须以**英文字母**开头：否则 `总容积(L)` 会被拆成 总容积 / `(L)`、
    #   `搁物架(个)` 会被拆成 `搁物架(个` / `)`（踩过，见 2026-09-29 日志）。
    _U = r"([A-Za-z][A-Za-z0-9/·.*()（）\-]*)"
    # ① 键(说明)单位   —— 例：产品尺寸(宽*高*深)mm
    m = re.match(r"^(.*?)[（(][^()（）]*[)）]" + _U + r"$", k)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    # ② 中文键+英文单位 —— 例：噪声dB(A)（必须在 ① 之后，否则会被误拆成 噪声dB / A）
    m = re.match(r"^(.*" + _CJK_RE.pattern + r")" + _U + r"$", k)
    if m and m.group(1).strip():
        return m.group(1).strip(), m.group(2).strip()
    # ③ 键(单位)       —— 例：总容积(L) / 搁物架(个) / 综合耗电量(kW·h/24h)
    m = re.match(r"^(.*?)[（(]([^()（）]{1,12})[)）]$", k)
    if m and m.group(1).strip():
        unit = m.group(2).strip()
        # 中文单位只认极短的（个/人/天…），更长的一律当「说明文字」不拆
        if _CJK_RE.search(unit) and len(unit) > 2:
            return k, ""
        return m.group(1).strip(), unit
    return k, ""


def _norm_spider_groups(groups):
    """爬虫参数分组 -> App 惯用格式（单位从键挪到值），同结构返回 [(组名, [(k, v)])]。

    值里已经带了这个单位就不重复追加（幂等）。
    """
    out = []
    for g, kv in groups or []:
        nkv = []
        for k, v in kv:
            k2, unit = _split_key_unit(k)
            v2 = str(v or "").strip()
            if unit and unit not in v2:
                v2 = v2 + unit
            nkv.append((k2 or k, v2))
        if nkv:
            out.append((g, nkv))
    return out


def spider_backfill(info, log=None):
    """【遗留数据修复】本地已有素材但 props 为空时，用爬虫按 info["url"] 补一次参数并回写 info.json。

    场景：老型号的详情页没有 JSON-LD additionalProperty（如 2019 年页面），旧路径只能拿到 0 参数，
    描述里就会写「见详情」。这些素材早已落盘，`_work` 不会重抓 —— 故在「本地载入」时补这一步。

    ⚠️ 只在 props 为空时调用；有参数的老素材完全不触发，零影响。失败原样返回。
    """
    if not info or info.get("props"):
        return info                    # 已有参数：绝不改动（本函数只服务「参数为空」的遗留素材）
    url = info.get("url") or ""
    site = _site_of_url(url)
    if not (url and site):
        if log:
            log("参数为空，但素材里没有可用的官网链接，跳过补抓")
        return info
    # 顺便再搜一次拿官网标签/卖点；但只有当它命中的就是同一个页面时才采用，避免张冠李戴
    prod = None
    try:
        _s, _u, _p = spider_locate(info.get("model") or "", log=None)
        if _p and (_p.get("source_url") or "") == url:
            prod = _p
    except Exception:
        prod = None
    if log:
        log("参数为空，用爬虫按官网链接补抓参数 ...")
    return spider_enrich_info(info, site, url, prod, log=log)


def spider_enrich_info(info, site, url, prod=None, log=None):
    """用 official_spider 抓该详情页的介绍原文/分组参数/说明书/官网标签，增量补充进 info。

    · **不覆盖已有 props**（保持文案与版式不变）；仅当 props 为空时才用爬虫参数兜底。
    · 新增字段 params_groups / intro / labels / manual_url —— 旧代码不读，属纯增量。
    · 结束后重写 info.json。
    """
    if not info or not url or site not in _SPIDER_SITE_HOSTS:
        return info
    try:
        import pei_spider as _osp
    except Exception:
        return info
    try:
        groups, intro, title, book, price = _osp.fetch_detail(url, site)
    except Exception:
        return info

    if groups:
        # 单位从键挪到值，统一成 App 惯用格式（总容积(L)=309 -> 总容积=309L），
        # 这样描述模板与 4_核心参数.jpg 的显示格式与旧路径一致
        ngroups = _norm_spider_groups(groups)
        info["params_groups"] = [[g, [[k, v] for k, v in kv]] for g, kv in ngroups]
        if not (info.get("props") or []):
            flat = [(k, v) for _g, kv in ngroups for k, v in kv
                    if not any(x in k for x in ("净重", "毛重"))]
            if flat:
                info["props"] = flat
                if log:
                    log("参数为空，已用爬虫参数兜底（%d 项，单位已归一化）" % len(flat))
    if intro:
        info["intro"] = intro
    # 说明书：详情页 bookAppFile 常缺，检索接口的 bookUrl 才是主要来源（prod 里带）
    manual = book or (prod or {}).get("manual_url") or ""
    if manual:
        info["manual_url"] = manual
    if prod:
        if prod.get("labels"):
            info["labels"] = list(prod["labels"])
        if prod.get("selling"):
            info["selling"] = list(prod["selling"])
    if title and not info.get("title"):
        info["title"] = title
    try:
        if price and not int(info.get("price") or 0):
            info["price"] = int(float(price))
    except Exception:
        pass
    try:
        d = info.get("dir")
        if d:
            json.dump(info, open(os.path.join(d, "info.json"), "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
    except Exception:
        pass
    if log:
        log("爬虫补充：介绍 %d 字 / 参数组 %d / 标签 %d / 说明书 %s"
            % (len(info.get("intro") or ""), len(info.get("params_groups") or []),
               len(info.get("labels") or []), "有" if info.get("manual_url") else "无"))
    return info


def guess_cat(title):
    for k, v in [("冰箱", "冰箱"), ("冷柜", "冷柜"), ("洗衣", "洗衣机"), ("空调", "空调"),
                 ("热水器", "热水器"), ("电视", "电视"), ("洗碗", "厨电"), ("油烟", "厨电"),
                 ("燃气灶", "厨电"), ("酒柜", "酒柜"), ("冰吧", "冰吧")]:
        if k in title:
            return v
    return "电器"


# ---------------- 文案生成 ----------------
SPEC_RES = [r"\d+(?:\.\d+)?\s*升", r"\d+(?:\.\d+)?\s*匹", r"\d+(?:\.\d+)?\s*公斤",
            r"\d+\s*英寸", r"\d+\s*L(?![A-Za-z0-9])"]


def pick_spec(text):
    if not text:
        return ""
    for pat in SPEC_RES:
        m = re.search(pat, text, re.I)
        if m:
            return m.group(0).replace(" ", "").replace("L", "升")
    return ""


def prop_dict(props):
    d = {}
    for k, v in props:
        d.setdefault(k, v)
    return d


def sellpoints(info, exclude=None):
    """本地生成最多 6 条卖点（XianyuPeiwen Pro 原版方案 + 用户要求扩到 6）。
    2026-10-26 用户令：只要机器的卖点（从真实参数提炼），不要口号凑数——
    凑不满 6 条就少几条。exclude 为【参数】行已展示过的字符串集合，
    命中则跳过对应卖点，避免描述和参数复读同一个数字（如 10kg）。"""
    pd = prop_dict(info.get("props") or [])
    cat = info.get("cat") or ""
    pts = []
    spec = pick_spec(info.get("title") or "")
    blocked = set()
    if exclude:
        for s in exclude:
            s = str(s).strip()
            if s:
                blocked.add(s)

    def find(*names):
        for n in names:
            for k, v in pd.items():
                if n in k:
                    return v
        return None

    def add(s):
        # 若卖点里的数字与【参数】已展示的重复 → 跳过（防复读参数）
        if blocked:
            for num in re.findall(r"\d+(?:\.\d+)?", s):
                if (num in blocked or num + "kg" in blocked or num + "升" in blocked
                        or num + "L" in blocked or num + "mm" in blocked
                        or num + "dB" in blocked):
                    return
        if s not in pts:
            pts.append(s)

    # ---------- 参数化卖点（2026-10-26 用户令：要机器的卖点，不要口号凑数）----------
    # 全品类从真实参数提炼；凑不满 6 条就少几条，绝不拿空话填。
    def num_of(*names):
        m = re.search(r"(\d+(?:\.\d+)?)", str(find(*names) or ""))
        return m.group(1) if m else ""

    def has_kw(*names):
        for n in names:
            for k, v in pd.items():
                if n in k or n in str(v):
                    return True
        return False

    size = find("尺寸", "外形尺寸")
    if size:
        m = re.match(r"(\d{3})", size)
        if m and int(m.group(1)) < 600:
            add("%smm超薄机身" % m.group(1))
    # 容量/容积：冰箱讲分储、热水器讲水量、洗衣机讲一次洗多少
    vol = num_of("总容积", "额定容量", "洗涤容量", "容积", "容量", "额定升数", "出水量")
    if vol:
        if "热水器" in cat:
            add(vol + "升容量")
        elif "洗衣机" in cat or "干衣机" in cat:
            # 2026-11-23 双筒口径：上筒4kg+下筒8kg 相加=12（C8U12G3「4kg 洗涤容量」实锤）
            _kcv = ""
            for _nk in ("洗涤容量", "额定容量", "容量"):
                for _pk, _pv in pd.items():
                    if _nk in str(_pk):
                        _kcv = kg_cap(_pv)
                        if _kcv:
                            break
                if _kcv:
                    break
            add(("%skg 洗涤容量" % _kcv) if _kcv else (vol + "kg 洗涤容量"))
        elif "洗碗机" in cat:
            # 2026-11-22 CWC16-B07BK 实锤：「装载容量 8套/每抽屉」是抽屉口径，
            # 双子抽屉 8×2=16 跟标题 16套 打架——每抽屉值不进容量胶囊
            _raw_v = ""
            for _nk in ("装载容量", "洗涤容量", "套数", "容量"):
                for _pk, _pv in pd.items():
                    if _nk in str(_pk) and "每抽屉" in str(_pv):
                        _raw_v = str(_pv)
                        break
                if _raw_v:
                    break
            if not _raw_v:
                add(vol + "套容量")
        else:
            # 冰箱才讲「分储」（2026-11-27 ZQD100F-C7MAGU1 消毒柜实锤：通用分支
            # 把 100升消毒柜写成「冷藏冷冻分储」冰箱话术——真参数池 0 证据）
            if "冰箱" in cat or "冷柜" in cat or "冰柜" in cat:
                add(vol + "升总容积，冷藏冷冻分储")
            else:
                add(vol + "升大容量")
    # 加热/动力类（2026-11-14 用户令「加热快怎么个快法要写明白」：按容量算整胆时间）
    heat = num_of("加热功率", "额定功率", "输入功率")
    if heat and "热水器" in cat:
        _L = _P = None
        try:
            _L = float(re.search(r"\d+(?:\.\d+)?", vol or "").group(0))
        except Exception:
            _L = None
        try:
            _P = float(heat)
        except Exception:
            _P = None
        if _L and _P and _P > 0:
            # 整胆 20→75℃（ΔT=55）：t = 1.163*L*ΔT/P 小时，向下取到 0.5 小时，只说算得出的
            _t = 1.163 * _L * 55.0 / _P
            _t = max(0.5, int(_t * 2) / 2.0)
            add("%sW 加热，整胆水约 %s 小时烧热" % (heat, ("%g" % _t)))
        else:
            add(heat + "W 加热功率")
    elif heat and ("烤箱" in cat or "微波" in cat):
        add(heat + "W 功率")
    pizz = find("匹数", "制冷量")
    inv_kw = has_kw("变频")
    if "空调" in cat and pizz:
        add(str(pizz) + "，变频恒温" if inv_kw else str(pizz))
    # 冰箱/冷柜特有
    ee = find("能效等级", "能效")
    if ee:
        # 2026-11-14 用户令「省电要有依据」：把等级摆前面，不跟别家比
        add(str(ee).strip() + "能效，节能省电")
    noise = find("噪声", "噪音", "声功率")
    if noise:
        _nz = str(noise).strip()
        if not re.search(r"dB|分贝", _nz):
            _nz += " 分贝"
        add("运行噪音 " + _nz)
    cool = find("制冷方式")
    if cool and "风冷" in str(cool):
        add("风冷无霜，不用手动除霜")
    comp = find("压缩机", "定变频", "电机类型")
    if comp and "变频" in str(comp):
        if "洗衣机" in cat:
            add("变频电机")
        elif "空调" in cat:
            add("变频恒温")
        else:
            add("变频压缩机，控温稳定")
    # 洗衣机特有
    if "洗衣机" in cat:
        if has_kw("烘干", "洗烘"):
            add("洗烘一体")
        if has_kw("除菌", "杀菌", "除螨"):
            add("带除菌洗")
        if has_kw("直驱"):
            add("直驱电机")
    # 空调特有
    if "空调" in cat:
        if has_kw("自清洁", "清洁"):
            add("自清洁内机")
        if has_kw("新风"):
            add("带新风功能")
    # 热水器特有
    if "热水器" in cat:
        if has_kw("防电墙", "安全防", "防干烧", "超温"):
            add("防电墙+防干烧，多重安全防护")
        if has_kw("保温", "发泡", "聚氨酯"):
            add("保温层整体发泡，保温持久")
        if has_kw("零冷水", "即热"):
            add("即开即热")
        ht = num_of("最高温度", "出水温度")
        if ht:
            add("最高水温 " + ht + "℃")
    # 油烟机特有
    if "油烟机" in cat:
        air = num_of("排风量", "风量", "排风")
        if air:
            add(air + " 立方米大风量")
    # 燃气灶/集成灶
    if "燃气灶" in cat or "集成灶" in cat:
        hs = num_of("热负荷", "额定热负荷", "火力")
        if hs:
            add(hs + "kW 热负荷")
        if has_kw("熄火保护"):
            add("带熄火保护，溢锅自动断气")
    # 净水器
    if "净水器" in cat:
        fl_ = num_of("通量", "额定净水量", "净水量")
        if fl_:
            add(fl_ + " 通量")
        if has_kw("除菌", "超滤", "过滤"):
            add("多级过滤，除菌除余氯")
    # 电视特有
    if "电视" in cat:
        inch = num_of("屏幕尺寸", "对角线尺寸")
        if inch:
            add(inch + "英寸大屏，客厅看着更过瘾")
        if has_kw("4K", "超高清", "8K"):
            add("超高清画质，细节看得清")
    # 智能通用
    if has_kw("WiFi", "WIFI", "wifi", "物联", "智能互联", "远程"):
        add("手机远程可控，出门在外也能管")
    # 品类禁写兜底（2026-11-14）：宁可少一条，也不留做不到的承诺
    try:
        import copy_cats as _cc2
        _bans = _cc2.ban_words(cat or "", info.get("props") or [])
        pts = [p for p in pts if not any(b and b in p for b in _bans)]
    except Exception:
        pass
    return pts[:6]


CAT_WORDS = ("冰箱", "冷柜", "洗衣机", "干衣机", "空调", "热水器", "油烟机",
             "洗碗机", "烤箱", "燃气灶", "集成灶", "净水器", "电视", "消毒柜")


def cat_from_url(url):
    """URL → 品类中文：取最深匹配 CAT_CN 的路径段（/xjd/jsj/ → 净水器，2026-10-22）。"""
    path = re.sub(r"[?#].*$", "", url or "")
    segs = [s for s in path.split("/") if s]
    if segs and "." in segs[-1]:
        segs = segs[:-1]
    for s in reversed(segs):
        if s in CAT_CN:
            return CAT_CN[s]
    return ""


def cat_name(info):
    """品类名：优先用抓取的 cat，没有就从标题里认。
    「电器/家电」是抓取链的废词兜底（2026-11-17 HRF-H57SSEDU1 实锤标题出
    「571L风冷无霜电器」），当没认出来处理，走标题里的品类词。"""
    cat = (info.get("cat") or "").strip()
    if cat and cat not in ("电器", "家电"):
        return cat
    t = info.get("title") or ""
    for w in CAT_WORDS:
        if w in t:
            return w
    return cat or ""


def title_points(info):
    """标题用短卖点（4 字左右），从真实参数里提，不提假"""
    pd = prop_dict(info.get("props") or [])

    def find(*names):
        for n in names:
            for k, v in pd.items():
                if n in k:
                    return v
        return None

    pts = []
    comp = find("压缩机", "定变频", "电机类型")
    if comp and "变频" in str(comp):
        pts.append("变频节能")
    cool = find("制冷方式")
    if cool and "风冷" in str(cool):
        pts.append("风冷无霜")
    ee = find("能效等级", "能效")
    if ee and ("1级" in str(ee) or "一级" in str(ee)):
        pts.append("一级能效")
    vol = find("总容积", "容积", "洗涤容量", "容量")
    if vol:
        pts.append("大容量")
    noise = find("噪声", "噪音")
    if noise:
        pts.append("静音运行")
    heat = find("加热", "速热")
    if heat and "支持" in str(heat):
        pts.append("速热省时")
    if not pts:
        pts = ["正品保障", "品质保证", "售后无忧"]
    # 去重、补足 3 条
    uniq, seen = [], set()
    for p in pts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    fallback = ["变频节能", "大容量", "一级能效", "静音运行", "正品保障"]
    for f in fallback:
        if len(uniq) >= 3:
            break
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq[:3]


def kg_cap(vs):
    """容量值里的公斤数（2026-11-23 C8U12G3 实锤）：
    单值「8 kg」→ 8；双筒「上筒4 kg+下筒8 kg」→ 相加 = 12（总容量口径）。
    必须带 kg/公斤 单位才算，防止「容量范围 4-8」这类被误加。
    剥数字拼接会把 4+8 拼成 48（标题「48公斤大容量」元凶），禁用。"""
    nums = re.findall(r"(\d+(?:\.\d+)?)\s*(?:kg|公斤)", str(vs), re.I)
    if not nums:
        return None
    vals = [float(x) for x in nums]
    total = sum(vals) if len(vals) > 1 else vals[0]
    return int(total) if total == int(total) else total


def capacity_str(info):
    """容量（带单位），从真实参数提，没有则 ''。例：'243升' / '12L' / '10kg'"""
    pd = prop_dict(info.get("props") or [])
    keys = ["总容积", "公称容积", "容积", "洗涤容量", "热水产率", "额定容积",
            "容量", "水箱容积", "冷冻容积"]
    val = None
    for k in keys:
        if k in pd:
            val = str(pd[k]); break
    if val is None:
        # 键名带单位括号归一（2026-11-25 EC8005-MV5U1 官网登记「容积(L)=80」实锤：
        # 「容积」精确匹配撞不上「容积(L)」，标题四要素缺容量）
        def _nk(s):
            return re.sub(r"[（(].*?[)）]", "", str(s)).replace(" ", "").strip()
        _nkmap = {_nk(k): k for k in pd}
        for k in keys:
            _kk = _nkmap.get(k)
            if _kk is not None:
                val = str(pd[_kk])
                # 值是纯数字、单位长在键名括号里 → 把键名单位补到值尾（80 + L = 80L）
                if re.fullmatch(r"\d+(?:\.\d+)?", val.strip()):
                    _ku = re.search(r"[（(]([^\)）]{1,8})[)）]", str(_kk))
                    if _ku and re.fullmatch(r"(?i)(l|升|kg|公斤|m³|立方米)",
                                            _ku.group(1).strip()):
                        val = "%s%s" % (val.strip(), _ku.group(1).strip())
                break
        for v in pd.values():
            if re.search(r"\d", str(v)) and re.search(r"(升|L|kg|公斤|m³|立方米)", str(v)):
                val = str(v); break
    if not val:
        return ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(升|L|kg|公斤|m³|立方米)", val)
    if not m:
        return ""
    unit = m.group(2).replace("公斤", "kg")
    # 2026-11-23 双筒口径：上筒4kg+下筒8kg 相加=12kg（C8U12G3「4kg」实锤）
    if unit == "kg":
        _kc = kg_cap(val)
        if _kc:
            return "%skg" % _kc
    return m.group(1) + unit


def core_feature(info):
    """标题用的核心功能（单个最突出的卖点短语）。"""
    pd = prop_dict(info.get("props") or [])
    def find(*names):
        for n in names:
            for k, v in pd.items():
                if n in k:
                    return str(v)
        return ""
    if "风冷" in find("制冷方式"):
        return "风冷无霜"
    if "变频" in find("压缩机", "定变频", "电机类型"):
        return "变频节能"
    ee = find("能效等级", "能效")
    if ee and ("1级" in ee or "一级" in ee):
        return "一级能效"
    if find("总容积", "容积", "洗涤容量", "容量"):
        return "大容量"
    if find("噪声", "噪音"):
        return "静音运行"
    if "支持" in find("加热", "速热"):
        return "速热省时"
    cat = cat_name(info)
    return {"冰箱": "风冷无霜", "冷柜": "深冷锁鲜", "热水器": "强排恒温",
            "洗衣机": "变频洗护", "油烟机": "大吸力", "燃气灶": "聚能猛火",
            "空调": "变频冷暖", "洗碗机": "高温除菌", "净水器": "多级过滤"}.get(cat, "品质优选")


def type_word(info):
    """类型词（门数/安装方式/气源等），没有则 ''。例：'三门' / '强排式' / '嵌入式'"""
    pd = prop_dict(info.get("props") or [])
    cat = cat_name(info)
    def find(*names):
        for n in names:
            for k, v in pd.items():
                if n in k:
                    return str(v)
        return ""
    def first_hz(v):
        hz = re.findall(r"[一-鿿]{1,4}", v)
        return hz[0] if hz else ""
    if cat in ("冰箱", "冷柜"):
        return first_hz(find("箱门结构", "门数", "开门方式", "类型")) or ""
    if cat == "热水器":
        return first_hz(find("排烟方式")) or first_hz(find("气源类别")) or "燃气"
    if cat == "洗衣机":
        return first_hz(find("洗涤方式", "类型", "门类型")) or "滚筒"
    return {"油烟机": "顶吸", "燃气灶": "嵌入式", "空调": "壁挂",
            "洗碗机": "嵌入式", "净水器": "厨下", "消毒柜": "嵌入式"}.get(cat, "")


def titles(info):
    """标题格式（用户 2026-09-15 定稿）：品牌 + 容量 + 品类 + 核心功能 + 型号 + 成色 + 售后
    给 3 个版本（结构相同，措辞/顺序不同），版本 1 优先用。闲鱼标题上限 60 字符。
    例（海尔 243 升 三门冰箱）：
      1 海尔 243 升风冷无霜冰箱 BCD-243WGHC2E7XM 全新机 家用三门 全国联保
      2 全新机 海尔 243L 三门冰箱 风冷无霜 BCD-243WGHC2E7XM 原厂正品联保
      3 海尔三门冰箱 243 升 风冷无霜 BCD-243WGHC2E7XM 全新机 官方正品 全国联保
    """
    b = (info.get("brand") or "海尔").strip()
    model = (info.get("model") or "").strip()
    cat = cat_name(info)
    cap = capacity_str(info)              # '243升' / '12L' / '10kg'
    feat = core_feature(info)             # '风冷无霜'
    tw = type_word(info)                  # '三门' / '强排式' / ''

    cap_sp = (cap.replace("升", " 升") if cap else "")   # '243 升'
    cap_ns = (cap.replace("升", "L") if cap else "")     # '243L'

    def join(*parts):
        return " ".join(p for p in parts if p)

    # 版本1：品牌 容量 核心功能+品类 型号 成色 家用类型 售后
    mid1 = cap_sp + (feat + cat if feat else cat)        # '243 升风冷无霜冰箱'
    v1 = join(b, mid1, model, "全新机",
              ("家用" + tw) if tw else "", "全国联保")
    # 版本2：成色 品牌 容量(无空格) 类型+品类 核心功能 型号 售后(原厂)
    v2 = join("全新机", b, cap_ns, (tw + cat) if tw else cat, feat, model, "原厂正品联保")
    # 版本3：品牌+类型+品类 容量 核心功能 型号 成色简+售后（2026-11-14 用户令：全新原封除名）
    v3 = join((b + tw + cat) if tw else (b + cat), cap_sp, feat, model, "全新机", "官方正品全国联保")
    # 闲鱼标题按「字符数」计上限（每个汉字算 1 字），用字符数截断，避免中文被双算砍掉
    def clip(s, n=60):
        s = re.sub(r"\s+", " ", str(s)).strip()
        if len(s) <= n:
            return s
        c = s[:n]
        if " " in c:
            c = c[:c.rfind(" ")]
        return c.rstrip()
    return [clip(x) for x in (v1, v2, v3)]


def _truncate(s, width):
    """按视觉宽度截断（中文=2，英文/数字=1）。优先在词边界结束。"""
    s = re.sub(r"\s+", " ", str(s)).strip()
    cur, out = 0, []
    truncated = False
    for ch in s:
        w = 2 if ord(ch) > 127 else 1
        if cur + w > width:
            truncated = True
            break
        out.append(ch)
        cur += w
    res = "".join(out).rstrip()
    # 仅当真发生了截断时，才回退到上一个空格（避免误删完整串尾词）
    if truncated and " " in res:
        last = res.rfind(" ")
        if last > 0:
            res = res[:last].rstrip()
    return res


def desc_text(info, version, points=None):
    """商品描述（结构由用户指定）
    【品牌+规格品类】型号 xxx
    ·卖点1
    ·卖点2
    …最多 6 条（2026-10-26 用户令：只收机器参数卖点，不满就少几条；全空才落静态兜底）
    【参数】k v | k v | ...
    【官网价】xxx（本店售价见标价，全新机）
    【交易】先验后签收，破损拒收
    结尾 5 行话术（可发货地区随版本切换）
    version: 'N' 全国 / 'B' 报备（深圳、东莞、惠州、广州）
    points: 外部传入的卖点（AI 润色时用）；不给则按参数本地生成
    """
    props = list(info.get("props") or [])
    brand = (info.get("brand") or "海尔").strip()
    model = (info.get("model") or "").strip()
    title = (info.get("title") or "").strip()

    def find(*keys):
        for k, v in props:
            for key in keys:
                if key in str(k):
                    return str(v)
        return ""

    # 标题行：【品牌+规格品类】型号 xxx
    head = title if title.startswith(brand) else brand + title

    # 参数行：核心参数前 6 项（跳过结构件/铭牌类非核心项）
    skip = ("箱门结构", "面板材质", "控温方式", "额定电压", "额定频率",
            "搁物架", "果菜盒", "变温抽屉", "瓶座", "冷冻抽屉", "速冷速冻",
            "童锁", "包装尺寸", "上市时间", "型号")
    core = []
    for k, v in props:
        if not v or any(x in str(k) for x in skip):
            continue
        core.append("%s %s" % (k, v))
        if len(core) >= 6:
            break

    price = info.get("price") or 0
    lines = []
    lines.append("【%s】型号 %s" % (head, model))
    lines.append("")
    # 【参数】行已展示的数字/量词，传给卖点生成，避免描述和参数复读同一串数字
    excl = set()
    for c in core:
        v = c.split(" ", 1)[1] if " " in c else c
        if re.search(r"\d", v):
            excl.add(v.strip())
            for num in re.findall(r"\d+(?:\.\d+)?", v):
                excl.add(num)
    # XianyuPeiwen Pro 原版链路 + 2026-10-17 扩到 6 条：
    # AI 卖点优先取前 6 → 不足由本地池补齐（防重复+防特征词复读）。
    # 2026-10-26 用户令二修：参数行数字进排除表后卖点常被毙光（热水器 60/2200/75/1
    # 全在参数里 → 实锤），宁可数字跟参数行重复也绝不落服务口号——
    # 第一轮严排除 → 不足 3 条第二轮不排除重跑 → 仍空才落 3 条服务兜底（最后手段）。
    sp = [str(x).strip() for x in (points or []) if str(x).strip()][:6]
    # 本地候选与已有卖点撞同一特征（风冷/能效/变频…）→ 跳过，防语义复读
    _FEAT = ("风冷", "无霜", "除霜", "能效", "省电", "变频", "定频", "温区",
             "容量", "分储", "静音", "噪音", "冷藏", "冷冻")
    for x in sellpoints(info, exclude=excl):
        if len(sp) >= 6:
            break
        if x in sp:
            continue
        if any(w in x and any(w in e for e in sp) for w in _FEAT):
            continue
        sp.append(x)
    if len(sp) < 3:
        for x in sellpoints(info):
            if len(sp) >= 6:
                break
            if x in sp:
                continue
            if any(w in x and any(w in e for e in sp) for w in _FEAT):
                continue
            sp.append(x)
    if not sp:
        sp = ["全新机，官方正品", "全国联保，售后有保障",
              "现货在仓，下单即发"]
    for pt in sp:
        lines.append("·" + pt)
    lines.append("")
    lines.append("【参数】%s" % (" | ".join(core) if core else "见详情"))
    lines.append("【官网价】%s（本店售价见标价，全新机）"
                 % (str(int(price)) if price else "见标价"))
    lines.append("【交易】先验后签收，破损拒收")
    lines.append("")
    lines.append("%s官方正品，全新机，全国联保" % brand)
    lines.append("品质保证，放心购买，售后无忧")
    lines.append("支持专业送货安装，当地售后师傅上门安装")
    lines.append("让您买得放心，用得安心！")
    lines.append("可发货地区：%s" % ("全国" if version == "N"
                              else "深圳、东莞、惠州、广州"))
    return "\n".join(lines)


# ---------- 文案新口径 v2（2026-10-21，用户另一会话定稿「调整洗衣机首图文案」） ----------
# 标题三档全闲鱼话术角度（全新未拆/样机清仓/特价处理），结构=品牌+卖点+型号，
# 品牌开头、型号收尾；正文一话一行 + 结尾承诺句，不带【参数】【官网价】【交易】和发货句。
# 卖点短语全部取自抓到的真实参数（容量/核心功能/能效/噪音/面板），取不到就留空 —— 防编造。

def _v2_sellsets(info):
    """v2 标题用的三组卖点短语：s1=容量+核心功能+类型（主推）、s2=变频+能效、
    s3=面板材质+噪音静音。全部来自真实参数/标题，取不到返回空串由调用方兜底。
    注意：core_feature 最后有品类通用词兜底（冰箱→风冷无霜等）——没 props 撑腰
    就是编造，v2 里 props 为空时禁用（宁可标题短也不上空话，2026-10-21）。"""
    cap = capacity_str(info) or ""
    tw = type_word(info) or ""
    feat = (core_feature(info) or "") if (info.get("props") or []) else ""
    s1 = (cap + feat + tw).strip()
    pd = prop_dict(info.get("props") or [])
    s2 = ""
    _inv = ""
    for k, v in pd.items():
        ks, vs = str(k), str(v)
        if "变频" in ks:
            if "定频" in vs or vs in ("否", "无"):
                _inv = "定频"
            elif vs in ("是", "直流变频", "全自动变频") or "变频" in vs:
                _inv = "变频"
    _ee = str(pd.get("能效等级") or pd.get("能效") or "").strip()
    if _ee:
        _ee = _ee.replace("1", "一").replace("2", "二").replace("3", "三")
        s2 = _inv + (_ee if "能效" in _ee else _ee + "能效")
    elif _inv:
        s2 = _inv
    s3 = ""
    _db = str(pd.get("噪音") or pd.get("噪声") or "").strip()
    if _db:
        _num = re.sub(r"[^0-9.]", "", _db)
        if _num:
            s3 = _num + "分贝静音"
    _panel = str(pd.get("面板材质") or "").strip()
    if _panel and _panel not in ("无", "否"):
        s3 = (_panel + s3) if s3 else _panel
    return s1, s2, s3


def titles_v2(info, kind='l'):
    """v2 标题（用户 2026-10-21 定稿）：三档闲鱼话术，品牌+卖点+型号。
    l：全新机 / 展示样机清仓 / 全新低价出；s：展示样机 / 样机清仓特惠 / 样机特价。"""
    b = (info.get("brand") or "海尔").strip()
    model = (info.get("model") or "").strip()
    s1, s2, s3 = _v2_sellsets(info)

    def join(*parts):
        return " ".join(p for p in parts if p)

    def clip(s, n=60):
        s = re.sub(r"\s+", " ", str(s)).strip()
        if len(s) <= n:
            return s
        c = s[:n]
        if " " in c:
            c = c[:c.rfind(" ")]
        return c.rstrip()

    if kind == 's':
        v1 = join(b, "展示样机", s1, model)
        v2t = join(b, "样机清仓特惠", s2 or s1, model)
        v3 = join(b, s3 or s1, "样机特价", model)
    else:
        v1 = join(b, "全新机", s1, model)
        v2t = join(b, "展示样机清仓", s2 or s1, model)
        v3 = join(b, s3 or s1, "全新低价出", model)
    return [clip(x) for x in (v1, v2t, v3)]


def desc_v2(info, kind='l', points=None):
    """v2 正文（用户 2026-10-21 定稿）：一话一行卖点 + 结尾承诺句，
    不带【参数】【官网价】【交易】和可发货地区（发货句用户明确不要）。"""
    brand = (info.get("brand") or "海尔").strip()
    model = (info.get("model") or "").strip()
    title = (info.get("title") or "").strip()
    head = title if title.startswith(brand) else brand + title
    lines = ["【%s】" % head if (model and model in head)
             else "【%s】型号 %s" % (head, model), ""]
    sp = [str(x).strip() for x in (points or []) if str(x).strip()][:6]
    if not sp:
        sp = [str(x).strip() for x in sellpoints(info) if str(x).strip()][:6]
    lines.extend(sp)
    lines.append("")
    if kind == 's':
        lines.append("展示样机，功能完好，%s全国联保，支持到货验货，破损可拒收。" % brand)
    else:
        lines.append("全新机，%s全国联保，支持到货验货，破损可拒收。" % brand)
    return "\n".join(lines)


# ★ 关窗时用来强制掐断「启动探测」的连接。
# 背景（2026-09-29）：_ai_indicator_init 在后台 daemon 线程里跑 zhipu_test，
# 若关窗时它正卡在 SSL recv（沙箱/代理下 urlopen 的 timeout 覆盖不到握手与读，
# 实测能拖到 30~60s），解释器关停会一直等这个线程 → 用户看到「关了窗口进程还在」。
# 这里把活连接登记在册，on_close 时 abort 掉，recv 立刻抛错、线程秒退。
_LIVE_CONN = []
_LIVE_CONN_LOCK = threading.Lock()


def abort_live_conns():
    """掐断所有在途的探测连接（关窗时调用，幂等、绝不抛异常）。"""
    with _LIVE_CONN_LOCK:
        conns, _LIVE_CONN[:] = list(_LIVE_CONN), []
    for c in conns:
        try:
            c.close()
        except Exception:
            pass


def zhipu_test(api_key):
    """测试智普 API 连通性：发一条最短请求，通返回 True"""
    if not api_key or not str(api_key).strip():
        return False
    try:
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=json.dumps({
                # 2026-10-29 升级 glm-4.5-flash（免费）；连通性探测必须关思考——
                # 4.5 系默认先烧 reasoning，max_tokens=4 会空正文误报连不上
                "model": "glm-4.5-flash",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 32,
                "thinking": {"type": "disabled"},
            }).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + str(api_key).strip()})
        # 必须带 context=ctx：打包后的 app 里没有 CA 证书包，
        # 走默认证书验证会在 macOS 上直接失败（表现就是「AI 连不上」）
        # timeout 收窄到 8s：这是启动时的探测不是业务请求，太长会拖住退出
        resp = urlopen_ssl(req, timeout=8)
        # 登记底层连接，供关窗时 abort（见 abort_live_conns 注释）
        try:
            with _LIVE_CONN_LOCK:
                _LIVE_CONN.append(resp)
        except Exception:
            pass
        with resp:
            d = json.loads(resp.read())
        return bool(d.get("choices"))
    except Exception:
        return False


def _zhipu_sellpoints(api_key, info, n=3, long=False):
    """让 AI 只产出卖点（标题用短词 / 描述用长句）。
    只返回卖点本身，其余文案由本地拼装 —— 这样格式、成色、参数、话术绝不会被 AI 改坏。
    失败返回 None。
    """
    if not api_key or not str(api_key).strip():
        return None
    b = (info.get("brand") or "").strip()
    model = (info.get("model") or "").strip()
    cat = cat_name(info)
    props = list(info.get("props") or [])
    prop_txt = "；".join("%s %s" % (k, v) for k, v in props[:10]) or "（暂无）"
    title_now = (info.get("title") or "").strip()

    if long:
        ask = ("请写 %d 条商品卖点，每条 12-20 个字，突出这台机器的核心优势，"
               "口吻亲切、能打动买家。" % n)
        rule = "只输出 %d 行，每行一条卖点正文（不要以·开头），不要序号，不要解释。" % n
    else:
        ask = ("请写 %d 个短卖点词，每个 3-5 个字，"
               "从真实参数里提炼（例如 变频节能 / 风冷无霜 / 大容量 / 一级能效）。" % n)
        rule = ("只输出 %d 行，每行一个词，不要序号，不要标点，不要解释。" % n)

    prompt = (
        "你是闲鱼家电卖家文案高手，卖海尔/卡萨帝/统帅全新机的正品家电。\n"
        "品牌: " + b + "\n"
        "型号: " + model + "\n"
        "品类: " + cat + "\n"
        "产品名: " + title_now + "\n"
        "官网价: " + str(info.get("price") or 0) + "元\n"
        "真实参数: " + prop_txt + "\n\n"
        # 官网产品介绍原文：让卖点措辞贴官方口径（没有则空串）
        + _intro_block(info)
        + ask + "\n\n"
        "硬性要求：\n"
        "1. 只能依据上面给出的真实参数来写，严禁编造参数里没有的功能\n"
        "2. 禁止 emoji、表情符号、任何特殊符号\n"
        "3. " + rule
    )
    try:
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=json.dumps({
                "model": "glm-4.5-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8,
                "thinking": {"type": "disabled"},
            }, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + str(api_key).strip(),
            })
        with urlopen_ssl(req, timeout=30) as r:
            d = json.loads(r.read())
        text = d["choices"][0]["message"]["content"]
    except Exception:
        return None

    out = []
    for raw in text.split("\n"):
        t = raw.strip()
        for pre in ("·", "-", "•", "*", "▪"):
            if t.startswith(pre):
                t = t[len(pre):].strip()
        # 只剥「序号」前缀（1. / 1、/ 1) ），别把卖点自带的数字（如 243升）吃掉
        t = re.sub(r"^\s*\d+\s*[.、)）:：]\s*", "", t).strip()
        t = re.sub(r"^\s*\d+\s+", "", t).strip()
        t = t.strip("「」《》\"\' 　")
        if t:
            out.append(t)
    return out[:n] if len(out) >= n else None


# ---------------- 门数（门结构）纠错 ----------------
# AI 软文案容易乱编门数（两门冰箱写成「三门设计」）。凡与真实门结构冲突，
# 一律替换为真实门数；无门数信息时不动。
_DOOR_WORDS = ["对开门", "十字四门", "十字", "多门", "五门", "四门",
              "三门", "双门", "两门", "单门", "法式", "T型"]

def real_door(info):
    """从 箱门结构/门数/开门方式 参数归一化出门数词。没有返回 None。"""
    pd = prop_dict(info.get("props") or [])
    s = str(pd.get("箱门结构") or pd.get("门数") or pd.get("开门方式") or "")
    if "两门" in s or "双门" in s:
        return "两门"
    if "对开" in s:
        return "对开门"
    if "十字" in s:
        return "十字四门"
    if "多门" in s:
        return "多门"
    if "三门" in s:
        return "三门"
    if "单门" in s:
        return "单门"
    return None

def _fix_door(s, real):
    if not s:
        return s
    for w in _DOOR_WORDS:
        if w != real and w in s:
            s = s.replace(w, real)
    return s

def sanitize_ai9_copy(obj, info):
    real = real_door(info)
    if not real or not isinstance(obj, dict):
        return obj
    for k, v in list(obj.items()):
        if isinstance(v, str):
            obj[k] = _fix_door(v, real)
        elif isinstance(v, list):
            for pair in v:
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    pair[0] = _fix_door(str(pair[0]), real)
                    pair[1] = _fix_door(str(pair[1]), real)
    return obj


# ---------------- AI 视觉选图（GLM-4V） ----------------
# 2026-11-11 用户令「Qwen3.5-4B 免费能用就用它首选」：视觉首选切硅基流动
# Qwen/Qwen3.5-4B（官方定价页实锤 输入¥0/输出¥0，原生多模态，实测判定/读数全对），
# 商汤 SenseNova 从视觉链撤下（常 429 限流、共享池排队）。
# 2026-11-12 安全整改：内置明文 sf key 已删除（公开仓库被抓走跑 DeepSeek 烧钱），
# 统一走 aikeys：env ML_SF_KEY → 本机 sf_key.txt → 本机缓存/云端 /aikey 下发。
SF_VISION_MODEL = "Qwen/Qwen3.5-4B"


def _sf_key():
    """硅基流动 key（见 aikeys：env → 本机 txt → 缓存/云端下发）。拿不到返回空串=关掉视觉 AI。"""
    try:
        import aikeys
        return (aikeys.sf_key() or "").strip()
    except Exception:
        return ""


def _qwen_vision(api_key, data_url, prompt):
    """硅基流动 Qwen3.5-4B 多模态看图（OpenAI 兼容，api.siliconflow.cn，免费模型）。
    注意：4B 是思考型模型，reasoning 也占 token，max_tokens 给足防截断；
    content 可能带 ```json 围栏/前导空行——消费方已有正则提取，不用在这剥。"""
    req = urllib.request.Request(
        "https://api.siliconflow.cn/v1/chat/completions",
        data=json.dumps({
            "model": SF_VISION_MODEL,
            "messages": [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": prompt}]}],
            "temperature": 0.3,
            "max_tokens": 2500,
        }, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + str(api_key).strip(),
                 "User-Agent": "Mozilla/5.0"})
    with urlopen_ssl(req, timeout=120) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


# ---------------- 视觉请求全局限流门（2026-11-04 用户令 C：排队 + 429 退避重试） ----------------
# 商汤/智谱免费额度都是滚动窗口：宝窗+婷窗+文案同时跑时十几个请求瞬间打出去，
# 几秒烧光额度自己制造 429。全局队列保证任意两次视觉 HTTP 请求最小间隔；
# 撞 429 不再直接切通道（切过去也限流），同通道退避重试用完才换。
_VISION_GATE_LOCK = threading.Lock()
_VISION_GATE_LAST = [0.0]
_VISION_GAP = 1.1          # 两次视觉请求最小间隔（秒）
_VISION_429_TRIES = 3      # 同通道最多试 3 次（原 1 次 + 退避重试 2 次）


def _vision_gate():
    """全局排队：进程内锁 + 跨进程文件锁（2026-11-10 表格令：宝窗/婷窗双开
    共享同一出口额度，任意两次视觉 HTTP 请求至少隔 _VISION_GAP 秒）。"""
    with _VISION_GATE_LOCK:
        _wait = _VISION_GAP - (time.monotonic() - _VISION_GATE_LAST[0])
        if _wait > 0:
            time.sleep(_wait)
        _VISION_GATE_LAST[0] = time.monotonic()
        _vision_gate_file()


def _vision_is_rate(e):
    """限流判定（2026-11-10 表格令收窄）：只认 HTTP 429，或响应体/报错文案里
    明确的 rate limit / quota / too many request / 1302 / 限流 / 额度 字样。
    超时、空 content、网络错误、鉴权失败不再被贴限流标签——真故障立即抛别拖。"""
    if getattr(e, "code", None) == 429:
        return True
    s = (_vision_err_body(e) + " " + str(e)).lower()
    return ("429" in s or "rate limit" in s or "too many request" in s
            or "requests per" in s or "quota" in s or "1302" in s
            or "限流" in s or "额度" in s or "配额" in s)


def _vision_err_body(e):
    """HTTPError 响应体原文（各家 429 的 error.type/message/request_id 都在 body 里）。"""
    try:
        raw = e.read()
    except Exception:
        return ""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8", "replace")
        except Exception:
            raw = str(raw)
    return str(raw or "")


def _vision_429_detail(e):
    """429 响应体结构化（2026-11-10 表格令）：坐实是 rate 还是 quota 还是出口代理。
    返回 (tag, 明细串)，tag ∈ rate / quota / unknown。"""
    body = _vision_err_body(e)
    tag, det = "unknown", ""
    try:
        j = json.loads(body)
        err = j.get("error") if isinstance(j.get("error"), dict) else {}
        det = "type=%s msg=%s code=%s req_id=%s" % (
            err.get("type") or j.get("type") or "?",
            (err.get("message") or j.get("message") or "?")[:120],
            err.get("code") or j.get("code") or "?",
            err.get("request_id") or j.get("request_id") or "?")
        low = json.dumps(j, ensure_ascii=False).lower()
        if "quota" in low or "balance" in low or "arrears" in low or "额度" in low:
            tag = "quota"
        elif "rate" in low or "频繁" in low or "限流" in low or "1302" in low:
            tag = "rate"
    except Exception:
        det = (body or str(e))[:200]
    return tag, det


def _vision_gate_file():
    """跨进程闸（2026-11-10 表格令「先把并发压到 1 路」）：宝窗+婷窗双开共享同一
    商汤/智谱出口额度，进程内闸管不到隔壁进程。锁住 gate 时间戳文件再读写，
    两个进程的视觉请求至少隔 _VISION_GAP 秒；锁不可用（平台差异/被占超 10s）
    就退化只走进程内闸，绝不因闸失败拦住出图。"""
    try:
        from paths import wp
        p = wp("_vision_gate.ts")
    except Exception:
        return
    try:
        f = io.open(p, "a+")
    except Exception:
        return
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)   # 阻塞抢锁（1s×10 次）
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = (f.read() or "").strip()
            try:
                last = float(raw)
            except Exception:
                last = 0.0
            _w = _VISION_GAP - (time.time() - last)
            if _w > 0:
                time.sleep(_w)
            f.seek(0)
            f.truncate()
            f.write("%.3f" % time.time())
            f.flush()
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
    except Exception:
        pass
    finally:
        try:
            f.close()
        except Exception:
            pass


def _vision_attempt(fn, api_key, data_url, prompt, log=None):
    """单通道视觉请求：过全局门 → 调用。分层处理（2026-11-10 表格令）：
    · HTTP 429 → 打出响应体 error.type/message/request_id，指数退避+抖动重试
      （6s→12s，不再死等固定秒数）；quota 类重试也没用，直接抛给上层切通道
    · 空 content / None（安全拒答、截断、超时被吞）→ 短隔重试，日志写「空返回」
      不再一律叫限流
    · 其他错误（超时/鉴权/网络）→ 立即抛，别把真故障拖成慢死"""
    _last_rate_exc = None
    for _i in range(_VISION_429_TRIES):
        _vision_gate()
        try:
            txt = fn(api_key, data_url, prompt)
        except Exception as e:
            if _i < _VISION_429_TRIES - 1 and _vision_is_rate(e):
                _tag, _det = _vision_429_detail(e) if getattr(e, "code", None) == 429 \
                    else ("rate", "")
                if _tag == "quota":
                    raise   # 额度烧完了，重试只是白等——立刻切兜底通道
                _back = min(6 * (2 ** _i), 15) + random.uniform(0, 1.5)
                try:
                    if log:
                        log("视觉请求限流（%s），等 %.0f 秒重试（第 %d/%d 次）%s"
                            % (_tag, _back, _i + 1, _VISION_429_TRIES - 1, _det))
                except Exception:
                    pass
                _last_rate_exc = e
                time.sleep(_back)
                continue
            raise
        if txt and str(txt).strip():
            return txt
        if _i < _VISION_429_TRIES - 1:
            try:
                if log:
                    log("视觉返回空 content（非限流），重试（第 %d/%d 次）"
                        % (_i + 1, _VISION_429_TRIES - 1))
            except Exception:
                pass
    if _last_rate_exc is not None:
        raise _last_rate_exc
    return None


def _vision_call(api_key, data_url, prompt, log=None):
    """视觉调用（2026-11-27 用户令「视觉选图只要 Qwen3.5-4B」）：只走硅基流动
    Qwen/Qwen3.5-4B 一条通道，GLM-4V/商汤兜底全部退役。没 key 返回 None，
    调用失败抛异常（调用方自行兜底）。"""
    _sfk = _sf_key()
    if not _sfk:
        return None
    return _vision_attempt(_qwen_vision, _sfk, data_url, prompt, log=log) or None

def is_junk_cut(f):
    """透明底「非机器图」守门（2026-10-24 实锤后加，选图规则层与 AI 视觉选图共用）：
    官网相册会混进二维码/能效标识/logo/白幽灵图标这类透明 PNG，_is_cutout 全过、
    bbox ar 又正好落进关门/开门带（BCD-351WLHD3E7SFU1 的 25.png 二维码当上机器图实锤）。
    拦四类：① 二维码（灰度跳变极多，实测 1212 vs 机器图 ≤274）
            ② 能效标识表（白底 + ≥3 段彩色横条；机器色块只有 1-2 段连片，实锤 segs=0）
            ③ 近空白/白幽灵（整图近白、无暗无彩——透明底白图标在影棚底=隐身）
            ④ 扁平单色 logo/图标（单一色相族占绝对多数、几乎无暗部；实物照片有光影层次）"""
    try:
        im = Image.open(f)
        w, h = im.size
        if w < 200 or h < 200:
            return False
        rgba = im.convert("RGBA")
        bg = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        im2 = Image.alpha_composite(bg, rgba).convert("RGB").resize((64, 64))
        px = list(im2.getdata())
    except Exception:
        return False
    n = len(px)
    gray = [(r * 299 + g * 587 + b * 114) // 1000 for r, g, b in px]
    trans = sum(1 for y in range(64) for x in range(1, 64)
                if abs(gray[y * 64 + x] - gray[y * 64 + x - 1]) > 70)
    if trans >= 650:                    # ① 二维码
        return True
    sat_rows = []
    for y in range(64):
        row = px[y * 64:(y + 1) * 64]
        sat = sum(1 for r, g, b in row
                  if max(r, g, b) - min(r, g, b) > 60 and not (r > 235 and g > 235 and b > 235))
        sat_rows.append(sat > 20)
    segs, prev = 0, False
    for s in sat_rows:
        if s and not prev:
            segs += 1
        prev = s
    colored = sum(1 for r, g, b in px
                  if not (r > 225 and g > 225 and b > 225) and not (r < 70 and g < 70 and b < 70))
    dark = n - colored - sum(1 for r, g, b in px if r > 225 and g > 225 and b > 225)
    if segs >= 3 and colored > n * 0.15:   # ② 能效标识表
        return True
    if colored < n * 0.02 and dark < n * 0.02:   # ③ 白幽灵/近空白
        return True
    # ④ 扁平单色 logo：主色相族占绝对多数且几乎无暗部（蓝色渐变圆标实锤：
    #   渐变会把单色桶打散成 41%+26%，相邻色相桶合并后 67% 就现形）
    hue_n = {}
    for r, g, b in px:
        mx, mn = max(r, g, b), min(r, g, b)
        if mx - mn > 60 and mx > 70:
            hue_n[int(_h_deg(r, g, b) // 20) % 18] = hue_n.get(int(_h_deg(r, g, b) // 20) % 18, 0) + 1
    if hue_n:
        # 相邻 20° 桶两两合并（渐变会跨桶）：取所有相邻对的最大和
        _best = 0
        for _i in range(18):
            _best = max(_best, hue_n.get(_i, 0) + hue_n.get((_i + 1) % 18, 0))
        if _best > n * 0.5 and dark < n * 0.05:
            return True
    # ⑤ 近无色图细分（2026-11-03 二次实锤 JSQ31-16CK5FQRGU1：旧 top3>0.93 一刀把
    #   白机身产品照也杀了——白机+浅灰光影+白底，灰阶必然集中在 3 桶内（01.png top3=0.989），
    #   正面照全被过滤后 AI 只能拿背面管路图充 front。改为三维细分，只杀真垃圾：
    #   · 扁平灰标：soft≈0（无任何柔和灰阶过渡，实测全为 0；实体图 ≥0.074）
    #   · 线稿图/尺寸图：256px 实体占比 <0.06（细线只占极小面积，实测 ≤0.041；白机照 ≥0.080）
    #   · 白幽灵小图标：不透明区 <0.15（线框幽灵实测 ≤0.081；白机抠图 ≥0.33）"""
    if dark < n * 0.02 and sum(hue_n.values()) < n * 0.02:
        try:
            _a = rgba.getchannel("A").resize((64, 64))
            _op = sum(1 for v in _a.getdata() if v > 128) / 4096.0
            _b = Image.alpha_composite(
                Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba
            ).convert("RGB").resize((256, 256))
            _body = sum(1 for r, g, b in _b.getdata()
                        if (r * 299 + g * 587 + b * 114) // 1000 <= 235) / 65536.0
            _g64 = [(r * 299 + g * 587 + b * 114) // 1000
                    for r, g, b in im2.getdata()]
            _soft = (sum(1 for y in range(64) for x in range(1, 64)
                         if 6 <= abs(_g64[y * 64 + x] - _g64[y * 64 + x - 1]) <= 48)
                     + sum(1 for y in range(1, 64) for x in range(64)
                           if 6 <= abs(_g64[y * 64 + x] - _g64[(y - 1) * 64 + x]) <= 48))
        except Exception:
            return False
        if _soft < 4096 * 0.02:        # 扁平灰方块标（无光影层次）
            return True
        if _body < 0.06:               # 尺寸线稿/白底细线图
            return True
        if _op < 0.15:                 # 白幽灵小图标
            return True
    return False


def _h_deg(r, g, b):
    """RGB → 色相角（0-360），算不出发 0"""
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    if d == 0:
        return 0
    if mx == r:
        return (60 * ((g - b) / d)) % 360
    if mx == g:
        return 60 * ((b - r) / d) + 120
    return 60 * ((r - g) / d) + 240


def _intro_block(info, limit=400):
    """把「官网产品介绍原文」整理成提示词里的一段，让 AI 写的卖点贴官方口径。

    原文来自第一步爬虫写进 info.json 的 "intro"（official_spider 抓的详情页正文）。
    没有就返回空串 —— 各调用点的行为与加这个功能之前完全一致，自然降级。
    """
    t = (info.get("intro") or "").strip()
    if not t:
        return ""
    t = re.sub(r"\s+", " ", t)[:limit]
    return ("官网产品介绍原文（**卖点措辞请尽量与官方口径一致**，可直接沿用其中的关键词和说法；"
            "但**严禁据此新增真实参数里没有的数字或功能**，也不要整段照抄）：\n" + t + "\n\n")


def zhipu_ai9_copy(api_key, info, version="N"):
    """让内嵌 AI（GLM-4-Flash）为 9 张套图自由发挥「软卖点」+ 各图标题/副标题。

    返回 dict（含 *_soft 软卖点对、*_title/*_sub 各图标题、tag、d_title），
    拿不到或解析失败返回 None。
    关键：只让 AI 写体验/场景向的「软」文案，真实参数一律取自 info，绝不外泄编造。
    """
    if not api_key or not str(api_key).strip():
        return None
    b = (info.get("brand") or "").strip()
    model = (info.get("model") or "").strip()
    cat = cat_name(info)
    props = list(info.get("props") or [])
    prop_txt = "；".join("%s %s" % (k, v) for k, v in props[:12]) or "（暂无）"
    door_txt = real_door(info)
    door_txt = ("真实门结构：%s（文案中的门数/结构必须与之一致，禁止写与之不符的「三门/对开门/四门」等说法）"
                % door_txt) if door_txt else ""

    prompt = (
        "你是闲鱼家电卖家的高级文案，为海尔/卡萨帝/统帅全新机正品家电写视觉海报文案。\n"
        "品牌: " + b + "\n型号: " + model + "\n品类: " + cat + "\n"
        "真实参数: " + prop_txt + "\n" + (door_txt + "\n" if door_txt else "") + "\n"
        # 官网产品介绍原文：让软卖点贴官方口径（没有则空串，行为与之前一致）
        + _intro_block(info)
        + "只输出一个 JSON 对象（不要 markdown 代码块、不要任何解释）。字段如下：\n"
        "{\n"
        '  "tag": "品类卖点短句，8-14字，如 大吸力·免拆洗自清洁",\n'
        '  "o_soft": [["短标签1","体验描述1"],["短标签2","体验描述2"],["短标签3","体验描述3"]],\n'
        '  "o_title": "容量/内部页大标题4-8字",\n'
        '  "o_sub": "该页一句话副标题10-18字",\n'
        '  "r_soft": [["短标签1","体验描述1"],["短标签2","体验描述2"],["短标签3","体验描述3"]],\n'
        '  "r_title": "尺寸/场景页大标题4-8字",\n'
        '  "r_sub": "该页一句话副标题10-18字",\n'
        '  "e_soft": [["短标签1","体验描述1"],["短标签2","体验描述2"],["短标签3","体验描述3"]],\n'
        '  "e_title": "能效/噪音页大标题4-8字",\n'
        '  "e_sub": "该页一句话副标题10-18字",\n'
        '  "d_soft": [["短标签1","体验描述1"],["短标签2","体验描述2"],["短标签3","体验描述3"]],\n'
        '  "d_title": "品质细节页大标题4-8字",\n'
        '  "m_soft": [["短标签1","体验描述1"],["短标签2","体验描述2"],["短标签3","体验描述3"]],\n'
        '  "angle_title": "立体角度页大标题4-8字",\n'
        '  "angle_sub": "该页一句话副标题10-18字"\n'
        "}\n\n"
        "硬性要求：\n"
        "1. 软卖点（*_soft 里的体验描述）可以生活化自由发挥，但不要编造上面参数里没有的具体数字；\n"
        "2. 短标签 2-5 个字，体验描述 4-10 个字；\n"
        "3. 禁止 emoji、表情符号和任何特殊符号；\n"
        "4. 每个 *_soft 给 3 组，字段名必须完全一致（含引号）；\n"
        "5. 只输出 JSON，不要多余内容。\n"
        "6. 门数/结构必须与「真实门结构」完全一致：凡涉及「门」的文案（如 XX设计 / XX冰箱 / 开X门）"
        "都用它，禁止写与之不符的「三门/对开门/四门」等说法。"
    )
    try:
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=json.dumps({
                "model": "glm-4.5-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.9,
                "thinking": {"type": "disabled"},
            }, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + str(api_key).strip(),
            })
        with urlopen_ssl(req, timeout=40) as r:
            d = json.loads(r.read())
        text = d["choices"][0]["message"]["content"]
    except Exception:
        return None
    # 解析 JSON（兼容模型偶尔包了 ```json ``` 的情况）
    try:
        obj = json.loads(text)
    except Exception:
        try:
            s = text.strip()
            if s.startswith("```"):
                s = s.split("```", 2)[1]
                if s.startswith("json"):
                    s = s[4:]
            obj = json.loads(s)
        except Exception:
            return None
    if not isinstance(obj.get("o_soft"), list):
        return None
    obj = sanitize_ai9_copy(obj, info)   # 纠正 AI 可能乱编的门数（如两门写成三门）
    return obj


def zhipu_polish_titles(api_key, info, base_titles):
    """标题结构固定（品牌+容量+品类+核心功能+型号+成色+售后），本地按真实数据拼装，
    不需要 AI 改格式；直接返回本地生成的 3 个版本。"""
    return titles(info)


def zhipu_polish_desc(api_key, info, version="N"):
    """AI 润色描述：AI 只出 6 条卖点（2026-10-17 跟随描述扩 6），整段由本地按模板拼装。
    参数/官网价/交易/结尾话术全部来自真实数据，AI 碰不到。AI 不足 6 条时
    desc_text 内部用本地池补齐。"""
    pts = _zhipu_sellpoints(api_key, info, 6, long=True)
    if not pts:
        return None
    return desc_text(info, version, points=pts)


# ---------------- 图片整理 ----------------
BAD_KEYWORDS = ("二维码",)


def publish_sorted(imgdir):
    """按文件名排的已下载图片，跳过临时产物（_publish_* 等）"""
    fs = []
    for f in os.listdir(imgdir):
        if not f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        if f.startswith("_"):
            continue  # 跳过 _publish_preview / _publish_task 等
        fs.append(f)
    return sorted(fs)


def make_cover(src_img, info, out_path):
    """拼版主图：产品整图铺满 + 底部深色信息条，任何底图都协调"""
    W = H = 1200
    bg = load_rgb(src_img)
    s = max(W / bg.width, H / bg.height)
    bg = bg.resize((int(bg.width * s) + 1, int(bg.height * s) + 1), Image.LANCZOS)
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2,
                  (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    img = bg.convert("RGBA")

    bw = int(H * 0.42)
    overlay = Image.new("RGBA", (W, bw), (18, 20, 24, 182))
    img.alpha_composite(overlay, (0, H - bw))
    th = 120
    over2 = Image.new("RGBA", (W, th), (18, 20, 24, 150))
    img.alpha_composite(over2, (0, 0))
    img = img if img.mode == "RGB" else load_rgb_img(img)
    dr = ImageDraw.Draw(img)

    def text(x, y, s, size, color="#ffffff", bold=True, anchor="la"):
        dr.text((x, y), s, font=font(size, bold), fill=color, anchor=anchor)

    spec = pick_spec(info.get("title") or "")
    text(40, 26, "%s · %s" % (info.get("brand") or "", info.get("title") or ""[:15]), 30, "#ffffff")
    text(40, 68, info.get("model") or "", 24, "#c9c9c9", bold=False)

    # 左侧：吸睛卖点（海报色带空间有限，固定取前 3 条，不随文案 6 条联动）
    y = H - bw + 36
    for p in sellpoints(info)[:3]:
        p = p if len(p) <= 20 else p[:19] + "…"
        dr.ellipse([44, y + 12, 56, y + 24], fill="#ffffff")
        text(70, y, p, 28, "#ffffff", bold=False)
        y += 54

    # 左侧下方：核心参数（2 列，齐全才叫首图）
    props = info.get("props", [])[:8]
    if props:
        py = y + 4
        text(40, py, "核心参数", 22, "#9ec5ef", bold=False)
        py += 32
        col_w = (W - 320 - 40 - 24) // 2
        for idx, (k, v) in enumerate(props):
            col = idx % 2
            row = idx // 2
            xx = 40 + col * (col_w + 24)
            yy = py + row * 30
            line = "%s：%s" % (k, v)
            line = line if len(line) <= 16 else line[:15] + "…"
            text(xx, yy, line, 21, "#d8dde6", bold=False)

    # 右侧：全新机徽章 + 官网价
    cx = W - 300
    dr.rounded_rectangle([cx, H - bw + 36, cx + 250, H - bw + 98], 12, fill="#ffffff")
    text(cx + 125, H - bw + 67, "全新机", 30, "#185fa5", anchor="mm")
    if info.get("price") or 0:
        text(cx, H - bw + 122, "官网价 ¥%s" % format(info.get("price") or 0, ","), 34, "#ffffff")

    # 已下架红标（红色块宽度自适应文字）
    dya = H - bw + 130
    if info.get("avail") == "UnStock":
        rb = "诚心要直接拍，大刀勿扰"
        rlen = int(font(22, True).getlength(rb))
        rpad = 22
        rbw = rlen + rpad * 2
        dr.rounded_rectangle([cx, dya + 56, cx + rbw, dya + 102], 10, fill="#e24b4a")
        text(cx + rbw // 2, dya + 79, rb, 22, "#ffffff", anchor="mm")
    img.save(out_path, quality=92)
    return out_path


def zhipu_make_sellpoints(api_key, info):
    """调智普 GLM-4-Flash 生成 3 条吸睛卖点（≤18 字）。失败返回 None。"""
    if not api_key or not api_key.strip():
        return None
    try:
        b, cat, model = info.get("brand") or "", info.get("cat") or "", info.get("model") or ""
        spec = pick_spec(info.get("title") or "")
        prompt = (
            "你是闲鱼家电营销高手，给" + b + cat + "写 3 条吸睛卖点（每条 ≤18 字）。\n"
            "型号：" + model + "\n"
            "规格：" + spec + "\n"
            "参数：" + "；".join("%s%s" % (k, v) for k, v in info.get("props", [])[:6]) + "\n\n"
            # 官网产品介绍原文：卖点措辞贴官方口径（没有则空串）
            + _intro_block(info, 300)
            + "要求：1. 口语化、冲击力强\n"
            "2. 突出全新机、官方正品、节能/大容量/静音等\n"
            "3. 3 条方向不同（容量/节能/品质）\n"
            "4. 直接输出 3 行，不要序号、不要解释"
        )
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=json.dumps({
                "model": "glm-4.5-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.85,
                "thinking": {"type": "disabled"},
            }, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key.strip(),
            })
        with urlopen_ssl(req, timeout=20) as r:
            d = json.loads(r.read())
        text = d["choices"][0]["message"]["content"]
        lines = []
        for raw in text.split("\n"):
            s = raw.strip().lstrip("1234567890.-、 ").strip()
            if s:
                lines.append(s[:20])
        return lines[:3] if len(lines) >= 3 else None
    except Exception:
        return None


def _best_product_img(img_dir):
    """从 img_dir 里挑最佳产品图：尺寸大 + 偏白底 + 排除二维码/小图"""
    best = None
    best_score = -1
    for f in os.listdir(img_dir):
        if not f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        if f.startswith("_") or f.startswith("0_") or f.startswith("AI_"):
            continue
        p = os.path.join(img_dir, f)
        try:
            im = Image.open(p)
            w, h = im.size
            if min(w, h) < 200:
                continue
            # 白度评分
            thumb = im.copy(); thumb.thumbnail((40, 40))
            avg = sum(sum(px) for px in thumb.getdata()) / (40 * 40 * 3)
            sz = os.path.getsize(p)
            # 评分：尺寸 * 白度权重
            score = (w * h) * (avg / 255.0) + min(sz / 1000.0, 500)
            if score > best_score:
                best_score = score
                best = p
        except Exception:
            pass
    return best


def ai_make_cover(info, sel_dir, api_key=None, out_name="0_AI_主图.jpg"):
    """AI 智能做主图：直接用第一张图 + 智普生成营销卖点 + 智能拼版"""
    os.makedirs(sel_dir, exist_ok=True)
    img_dir = info.get("dir") or sel_dir

    # 直接用第一张图（用户在图片区选的第一张），排除已有的 0_ 主图产物
    import glob
    candidates = glob.glob(os.path.join(sel_dir, "*.jpg")) + glob.glob(os.path.join(sel_dir, "*.png"))
    candidates = [p for p in candidates if not os.path.basename(p).startswith("0_")]
    if not candidates:
        return None, "发布图目录为空（没有可用产品图）"
    src = sorted(candidates)[0]  # 第一张

    # 2) AI 生成卖点（失败回落本地 sellpoints）
    pts = sellpoints(info)
    if api_key:
        ai_pts = zhipu_make_sellpoints(api_key, info)
        if ai_pts:
            pts = ai_pts

    # 3) 智能拼版（升级 make_cover）
    W = H = 1200
    bg = load_rgb(src)
    s = max(W / bg.width, H / bg.height)
    bg = bg.resize((int(bg.width * s) + 1, int(bg.height * s) + 1), Image.LANCZOS)
    bg = bg.crop(((bg.width - W) // 2, (bg.height - H) // 2,
                  (bg.width - W) // 2 + W, (bg.height - H) // 2 + H))
    img = bg.convert("RGBA")

    # 顶部品牌条
    th = 130
    over2 = Image.new("RGBA", (W, th), (18, 20, 24, 165))
    img.alpha_composite(over2, (0, 0))

    # 底部卖点条
    bw = int(H * 0.36)
    overlay = Image.new("RGBA", (W, bw), (18, 20, 24, 185))
    img.alpha_composite(overlay, (0, H - bw))

    # 右侧"全新机"徽章
    badge_w, badge_h = 200, 200
    cx = W - badge_w - 40
    cy = 30
    dr = ImageDraw.Draw(img)
    dr.rounded_rectangle([cx, cy, cx + badge_w, cy + badge_h], 20, fill="#dc2626")
    dr.text((cx + badge_w // 2, cy + 70), "全新", font=font(36, True), fill="white", anchor="mm")
    dr.text((cx + badge_w // 2, cy + 130), "未拆封", font=font(28, True), fill="white", anchor="mm")

    img = img if img.mode == "RGB" else load_rgb_img(img)
    dr = ImageDraw.Draw(img)

    def text(x, y, s, size, color="#ffffff", bold=True, anchor="la"):
        dr.text((x, y), s, font=font(size, bold), fill=color, anchor=anchor)

    # 顶部文字：品牌 + 标题
    raw_title = info.get("title") or info.get("cat", "")
    b = info.get("brand", "")
    if b and raw_title and not raw_title.startswith(b):
        show_title = b + " " + raw_title
    else:
        show_title = raw_title
    show_title = show_title if len(show_title) <= 18 else show_title[:17] + "…"
    text(40, 30, show_title, 32, "#ffffff")
    text(40, 78, "型号 " + info.get("model") or "", 22, "#c9c9c9", bold=False)

    # 卖点区（固定 3 条：海报色带版位是按 3 条设计的，不随描述 6 条联动）
    y = H - bw + 40
    for p in pts[:3]:
        p = p if len(p) <= 18 else p[:17] + "…"
        # 左侧白点
        dr.ellipse([44, y + 16, 44 + 14, y + 30], fill="#fbbf24")
        text(72, y, p, 32, "#ffffff", bold=False)
        y += 60

    # 价格徽章
    if info.get("price"):
        pxc = W - 300
        pyy = H - bw + 40
        dr.rounded_rectangle([pxc, pyy, pxc + 260, pyy + 110], 14, fill="#fbbf24")
        text(pxc + 130, pyy + 35, "官网价", 24, "#1f2937", anchor="mm")
        text(pxc + 130, pyy + 80, "¥" + format(info.get("price") or 0, ","), 36, "#dc2626", anchor="mm")

    # 已下架红标（红色块宽度自适应文字，右对齐）
    if info.get("avail") == "UnStock":
        dya = H - bw + 170
        rb = "诚心要直接拍，大刀勿扰"
        rlen = int(font(22, True).getlength(rb))
        rpad = 20
        rbw = rlen + rpad * 2
        bx = W - 40 - rbw
        dr.rounded_rectangle([bx, dya, bx + rbw, dya + 56], 12, fill="#dc2626")
        text(bx + rbw // 2, dya + 28, rb, 22, "#ffffff", anchor="mm")

    out_path = os.path.join(sel_dir, out_name)
    img.save(out_path, quality=93)
    # 原第一张图已融入主图，不再作为独立图片保留
    try:
        os.remove(src)
    except Exception:
        pass
    return out_path, "ok"


def make_zip(sel_dir, info, out_path):
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(sel_dir)):
            if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                z.write(os.path.join(sel_dir, f), "图片/" + f)
        z.writestr("文案_报备版.txt", desc_text(info, "B"))
        z.writestr("文案_全国版.txt", desc_text(info, "N"))
        z.writestr("标题.txt", "\n".join(titles(info)))
    return out_path


# ---------------- AI 选图 + 去黑底 ----------------
def _parse_ai9_copy(text):
    """解析 zhipu_ai9_copy / zhipu_product_read 返回的 JSON（兼容 ```json 包裹）。"""
    try:
        obj = json.loads(text)
    except Exception:
        try:
            s = text.strip()
            if s.startswith("```"):
                s = s.split("```", 2)[1]
                if s.startswith("json"):
                    s = s[4:]
            obj = json.loads(s)
        except Exception:
            return None
    if not isinstance(obj, dict) or not isinstance(obj.get("o_soft"), list):
        return None
    return obj


def zhipu_product_read(api_key, src_dir, info, log=None):
    """2026-11-10 用户令「不需要它去读图，要它去读抓取下来的信息」：
    改成纯文本读 info（品牌/型号/品类/真实参数 props + 官网产品介绍原文 intro），
    走免费文本模型 glm-4.5-flash（zhipu_ai9_copy 同一条路）提炼机型专属软卖点。
    不再拼版喂图、商汤/GLM-4V 视觉额度一分不烧。
    结果仍缓存到 <src_dir>/_ai_read_v2.json（跟旧视觉版同一个缓存文件，
    已有缓存的型号直接复用、一次都不用调）。无 key / 失败 -> 返回 None。"""
    if not api_key or not str(api_key).strip():
        return None
    p = Path(src_dir)
    cache = p / "_ai_read_v2.json"
    try:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        pass
    obj = zhipu_ai9_copy(api_key, info)          # 纯文本：只喂抓到的资料，GLM-4.5-Flash 免费
    if obj:
        try:
            cache.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    return obj


def defringe_white(im, lo=236):
    """白底素材边缘净化（2026-11-14 用户反馈「机图四周有色」）：
    官网原图机身端面是 245 左右的浅灰白，官网底色同为灰白看不出，
    贴到纸感白/纯白底上就显出一圈色块。这里做白场提拉——把 >=lo 的
    浅灰白像素推向 255（只动 RGB 不动 alpha），端面渐变与任意白底融合；
    机身深色轮廓/黄胶囊/黑面板全在 lo 以下，不受影响。
    2026-11-24 LES60H-AC5 实锤（蓝底/聚光通用）：官方透明渲染图（alpha
    通道抠好、边缘干净）没有色圈，拉白反把白机仅剩的机身渐变洗掉
    （≥250 像素 60%→69%），机器和白底融成一片——透明像素占比 >5%
    判为透明渲染图，原样返回不拉白；白底照片类照旧净化。"""
    try:
        im = im.convert("RGBA")
        a = im.split()[3]
        _ah = a.histogram()
        _at = sum(_ah)
        if _at and _ah[0] / float(_at) > 0.05:
            return im
        r, g, b = im.split()[:3]
        lut = []
        for v in range(256):
            if v >= lo:
                t = (v - lo) / max(1.0, 252 - lo)
                lut.append(int(v + (255 - v) * min(1.0, t * 2.5)))
            else:
                lut.append(v)
        return Image.merge("RGBA", (r.point(lut), g.point(lut), b.point(lut), a))
    except Exception:
        return im


# ---------------------------------------------------------------- 尺寸图底部红胶囊
HINT_RED = (229, 72, 46)
HINT_TEXT = {
    "blue": "拍前说下省市，先查当地库存",     # 蓝底：口语（2026-11-14 用户定稿）
    "spot": "先告知收货省市，帮您查当地库存",  # 聚光：客气（同上）
}


def hint_pill(im, text, y=1031, h=46, fs=31, padx=42, color=HINT_RED):
    """尺寸图底部单颗红底白字胶囊，整体水平居中。

    位置由用户 2026-11-14 定稿：胶囊 1008–1054，与上方 972 尺寸行留 22px、
    离底边 26px。之前 996 起过近，字视觉上像被压住（用户实锤「这个盖到字了」）。
    只画提示语，不加发货承诺（用户明令「当天/48小时内发这句不能加」）。"""
    if not text or im is None:
        return im
    try:
        from PIL import ImageDraw
        dr = ImageDraw.Draw(im)
        W = im.size[0]
        f = font(fs, True)
        tw = dr.textlength(text, font=f)
        maxw = W - 80 - padx * 2
        if tw > maxw:                       # 文案过长自动缩字号，绝不出画
            f = font(max(18, int(fs * maxw / max(1.0, tw))), True)
            tw = dr.textlength(text, font=f)
        x1 = (W - tw) / 2 - padx
        box = [x1, y - h / 2.0, x1 + tw + padx * 2, y + h / 2.0]
        dr.rounded_rectangle(box, radius=h / 2.0, fill=color)
        dr.text((W / 2.0, y), text, font=f, fill=(255, 255, 255), anchor="mm")
    except Exception:
        pass
    return im
