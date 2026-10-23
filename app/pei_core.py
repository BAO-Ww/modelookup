# -*- coding: utf-8 -*-
"""闲鱼配文 · 核心模块：官网抓取 + 文案生成 + 拼版主图"""
import os, re, json, ssl, gzip, time, shutil, zipfile, threading, sys, io, base64
from pathlib import Path
import urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from PIL import Image, ImageDraw, ImageFont

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

BLOCK_N = "海尔官方正品，全新未拆封，全国联保\n支持专业送货安装，售后无忧\n可发货地区：全国"
BLOCK_B = "海尔官方正品，全新未拆封，全国联保\n支持专业送货安装，售后有保障\n可发货地区：深圳、东莞、惠州、广州"

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
            with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
                d = r.read(n) if n else r.read()
            if d[:2] == b"\x1f\x8b":
                try:
                    d = gzip.decompress(d)
                except Exception:
                    pass
            return d
        except Exception:
            time.sleep(0.5)
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
        if os.path.exists(urlfile):
            urls = [u.strip() for u in open(urlfile, encoding="utf-8") if u.strip()]
        else:
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
        import official_spider as _osp
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
        import official_spider as _osp
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
    候选顺序=优先级（超薄>容量>能效>静音>风冷>变频），不够由软兜底凑满 6。
    exclude 为【参数】行已展示过的字符串集合，
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

    size = find("尺寸", "外形尺寸")
    if size:
        m = re.match(r"(\d{3})", size)
        if m and int(m.group(1)) < 600:
            add("%smm超薄机身，嵌入式安装更省空间" % m.group(1))
    vol = find("总容积", "容积", "容量")
    if vol:
        add(vol + "大容量，分储空间充足")
    ee = find("能效等级", "能效")
    if ee:
        add(ee + "能效，省电耐用")
    noise = find("噪声", "噪音", "声功率")
    if noise:
        add("运行噪音仅" + noise + "，静音不扰眠")
    cool = find("制冷方式")
    if cool and "风冷" in cool:
        add("风冷无霜，免手动除霜")
    comp = find("压缩机", "定变频")
    if comp and "变频" in comp:
        add("变频动力，恒温更稳定")
    # 兜底软卖点：与参数数字无关，保证凑满 6 条且不复读参数
    _SOFT = ("用料扎实，细节经得起细看", "安装省心，师傅上门服务",
             "日常好打理，省时省力", "全新未拆封，开箱即用",
             "官方正品渠道，支持当面验货", "现货在仓，下单即发不久等")
    for s in _SOFT:
        if len(pts) >= 6:
            break
        add(s)
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
    """品类名：优先用抓取的 cat，没有就从标题里认"""
    cat = (info.get("cat") or "").strip()
    if cat:
        return cat
    t = info.get("title") or ""
    for w in CAT_WORDS:
        if w in t:
            return w
    return ""


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
        for v in pd.values():
            if re.search(r"\d", str(v)) and re.search(r"(升|L|kg|公斤|m³|立方米)", str(v)):
                val = str(v); break
    if not val:
        return ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(升|L|kg|公斤|m³|立方米)", val)
    return (m.group(1) + (m.group(2).replace("公斤", "kg"))) if m else ""


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
      1 海尔 243 升风冷无霜冰箱 BCD-243WGHC2E7XM 全新未拆封 家用三门 全国联保
      2 全新未拆封 海尔 243L 三门冰箱 风冷无霜 BCD-243WGHC2E7XM 原厂正品联保
      3 海尔三门冰箱 243 升 风冷无霜 BCD-243WGHC2E7XM 全新原封全国联保
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
    v1 = join(b, mid1, model, "全新未拆封",
              ("家用" + tw) if tw else "", "全国联保")
    # 版本2：成色 品牌 容量(无空格) 类型+品类 核心功能 型号 售后(原厂)
    v2 = join("全新未拆封", b, cap_ns, (tw + cat) if tw else cat, feat, model, "原厂正品联保")
    # 版本3：品牌+类型+品类 容量 核心功能 型号 成色简+售后
    v3 = join((b + tw + cat) if tw else (b + cat), cap_sp, feat, model, "全新原封全国联保")
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
    …共 6 条（XianyuPeiwen Pro 方案 + 2026-10-17 扩到 6，软兜底凑满）
    【参数】k v | k v | ...
    【官网价】xxx（本店售价见标价，全新未拆封）
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
    # AI 卖点优先取前 6 → 不足 6 条由本地池补齐（防重复+防特征词复读）→ 静态兜底 6 条
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
    if not sp:
        sp = ["全新未拆封，官方正品", "全国联保，售后有保障", "支持专业送货安装",
              "先验货后签收，放心购", "机身小巧好安放", "现货在仓，下单即发"]
    for pt in sp:
        lines.append("·" + pt)
    lines.append("")
    lines.append("【参数】%s" % (" | ".join(core) if core else "见详情"))
    lines.append("【官网价】%s（本店售价见标价，全新未拆封）"
                 % (str(int(price)) if price else "见标价"))
    lines.append("【交易】先验后签收，破损拒收")
    lines.append("")
    lines.append("%s官方正品，全新未拆封，全国联保" % brand)
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
    l：全新未拆封 / 展示样机清仓 / 全新低价出；s：展示样机 / 样机清仓特惠 / 样机特价。"""
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
        v1 = join(b, "全新未拆封", s1, model)
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
        lines.append("全新未拆封，%s全国联保，支持到货验货，破损可拒收。" % brand)
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
                "model": "glm-4-flash",
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 4,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + str(api_key).strip()})
        # 必须带 context=ctx：打包后的 app 里没有 CA 证书包，
        # 走默认证书验证会在 macOS 上直接失败（表现就是「AI 连不上」）
        # timeout 收窄到 8s：这是启动时的探测不是业务请求，太长会拖住退出
        resp = urllib.request.urlopen(req, timeout=8, context=ctx)
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
        "你是闲鱼家电卖家文案高手，卖海尔/卡萨帝/统帅全新未拆封的正品家电。\n"
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
                "model": "glm-4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.8,
            }, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + str(api_key).strip(),
            })
        with urllib.request.urlopen(req, timeout=30, context=ctx) as r:
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
def _build_contact_sheet(cands, cols=4):
    """把候选图拼成带编号的拼版图，供视觉模型按角色选图。"""
    thumb, pad = 300, 16
    rows = (len(cands) + cols - 1) // cols
    cw, ch = thumb + pad * 2, thumb + pad * 2 + 44
    W, H = cols * cw + pad, rows * ch + pad
    cv = Image.new("RGB", (W, H), (232, 234, 238))
    dr = ImageDraw.Draw(cv)
    for i, f in enumerate(cands):
        r, c = divmod(i, cols)
        x, y = pad + c * cw, pad + r * ch
        try:
            im = Image.open(f).convert("RGB")
        except Exception:
            continue
        s = max(thumb / im.width, thumb / im.height)
        im = im.resize((max(1, int(im.width * s)), max(1, int(im.height * s))), Image.LANCZOS)
        cv.paste(im, (x + (thumb - im.width) // 2, y + (thumb - im.height) // 2))
        dr.rectangle([x, y, x + 46, y + 40], fill=(206, 54, 44))
        dr.text((x + 23, y + 20), str(i + 1), fill=(255, 255, 255), anchor="mm", font=font(26, True))
        dr.text((x + 4, y + thumb + pad), f.name, fill=(24, 24, 24), anchor="la", font=font(15, False))
    buf = io.BytesIO()
    cv.save(buf, "JPEG", quality=88)
    return buf.getvalue()


def _zhipu_vision(api_key, data_url, prompt):
    req = urllib.request.Request(
        "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        data=json.dumps({
            "model": "glm-4v-flash",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}}]}],
            "temperature": 0.3,
        }, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + str(api_key).strip()})
    with urllib.request.urlopen(req, timeout=60, context=ctx) as r:
        d = json.loads(r.read())
    return d["choices"][0]["message"]["content"]


def _parse_pick(text, cands):
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("```", 2)[1]
        if s.startswith("json"):
            s = s[4:]
    try:
        obj = json.loads(s)
    except Exception:
        return None
    if not isinstance(obj, dict):
        return None
    def name_of(v):
        try:
            idx = int(v) - 1
        except Exception:
            return None
        return cands[idx].name if 0 <= idx < len(cands) else None
    lab = str(obj.get("scene_label") or "").strip()
    if lab and lab != "无" and not lab.endswith(("实拍", "实景", "安装")):
        lab = lab + "实拍"
    return {
        "front": name_of(obj.get("front")),
        "angle": name_of(obj.get("angle")),
        "open": name_of(obj.get("open")),
        "scene": name_of(obj.get("scene")),
        "scene_label": (lab if lab and lab != "无" else None),
        "scene_sub": str(obj.get("scene_sub") or "").strip() or None,
    }


def zhipu_pick_images(api_key, src_dir, kind, log=None):
    """用 GLM-4V 看候选图，按角色（正面/角度/开门/实景）选图。返回 dict：
    front/angle/open/scene = 文件名或 None；scene_label/scene_sub = 实景页标题。
    结果缓存到 <src_dir>/_ai_pick.json，避免每次点发布图都调视觉模型。
    拿不到 key / 调用失败 / 候选太少 → 返回 None（调用方回退规则选图）。"""
    if not api_key or not str(api_key).strip():
        return None
    p = Path(src_dir)
    cands = []
    for f in sorted(p.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        try:
            im = Image.open(f); w, h = im.size
        except Exception:
            continue
        if w < 220 or h < 220:
            continue
        if w / h < 0.18 or w / h > 5.5:
            continue
        cands.append(f)
    if len(cands) < 3:
        return None
    cache = p / "_ai_pick_v2.json"   # v2：提示词加了「front 必须关门」，旧缓存作废
    try:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        pass
    sheet = _build_contact_sheet(cands)
    data_url = "data:image/jpeg;base64," + base64.b64encode(sheet).decode("ascii")
    prompt = (
        "这是%s的商品图拼版，每张左上角有红色编号。请按角色选出编号：\n"
        "1) front：产品整机正面（白底/透明/干净背景，能看到完整机器正面）。"
        "门/盖必须处于关闭状态——门开着的图只能填 open，绝不填 front\n"
        "2) angle：立体角度或侧面（门也关着，能看到机器厚度或斜视角，与正面不同）\n"
        "3) open：开门或看到内部（门打开/看到内胆或内部结构的图）\n"
        "4) scene：真实家居/阳台/卫生间/厨房的实景照片（人生活的环境里摆着这台机器）。\n"
        "   注意：纯参数图、功能原理图、内部结构渲染图、效果图、宣传长图都【不算】实景，请填 null。\n"
        "只回答一个 JSON：{\"front\":编号,\"angle\":编号,\"open\":编号,\"scene\":编号或null,"
        "\"scene_label\":\"阳台实拍/厨房实拍/卫生间实拍/客厅实拍/安装实拍/无\","
        "\"scene_sub\":\"一句话说明该实景\"}\n"
        "若某类没有合适图就填 null；scene_label 在没有实景时填 无。"
    ) % kind
    try:
        if log:
            try:
                log("正在用 AI 视觉选图 ...")
            except Exception:
                pass
        txt = _zhipu_vision(api_key, data_url, prompt)
        obj = _parse_pick(txt, cands)
        if obj:
            cache.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            return obj
    except Exception:
        pass
    return None


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
        "你是闲鱼家电卖家的高级文案，为海尔/卡萨帝/统帅全新未拆封正品家电写视觉海报文案。\n"
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
                "model": "glm-4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.9,
            }, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + str(api_key).strip(),
            })
        with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
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

    # 右侧：全新未拆封徽章 + 官网价
    cx = W - 300
    dr.rounded_rectangle([cx, H - bw + 36, cx + 250, H - bw + 98], 12, fill="#ffffff")
    text(cx + 125, H - bw + 67, "全新未拆封", 30, "#185fa5", anchor="mm")
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
            "2. 突出全新未拆封、官方正品、节能/大容量/静音等\n"
            "3. 3 条方向不同（容量/节能/品质）\n"
            "4. 直接输出 3 行，不要序号、不要解释"
        )
        req = urllib.request.Request(
            "https://open.bigmodel.cn/api/paas/v4/chat/completions",
            data=json.dumps({
                "model": "glm-4-flash",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.85,
            }, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + api_key.strip(),
            })
        with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
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

    # 右侧"全新未拆封"徽章
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
def _has_qr(path):
    """pyzbar 检测二维码（可选依赖，没装就当没二维码）"""
    try:
        from pyzbar.pyzbar import decode as _decode
    except Exception:
        return False
    try:
        im = load_rgb(path)
        im.thumbnail((800, 800))
        return len(_decode(im)) > 0
    except Exception:
        return False


def _image_score(path):
    """对单图打分：越大越好。综合 文件大小 / 尺寸 / 平均亮度(白度) / 比例(正方形加分) / 黑底惩罚"""
    try:
        im = load_rgb(path)
        w, h = im.size
        if min(w, h) < 200 or max(w, h) < 300:
            return None, "尺寸太小"
        sz = os.path.getsize(path)
        if sz < 20_000:
            return None, "文件太小"
        # 缩略求平均亮度
        thumb = im.copy()
        thumb.thumbnail((40, 40))
        pixels = list(thumb.getdata())
        avg = sum(sum(p) for p in pixels) / (40 * 40 * 3)  # 0-255
        # 长宽比
        aspect = min(w, h) / max(w, h)  # 0-1
        # 综合分
        score = 0.0
        score += min(sz / 200_000.0, 1.0) * 100      # 文件大
        score += aspect * 60                            # 偏正方形
        # 白底加分；中度暗中度惩罚；黑底大扣
        if avg > 200:
            score += 120
        elif avg > 150:
            score += 60
        elif avg > 100:
            score += 0
        elif avg > 60:
            score -= 80
        else:
            score -= 220
        # 分辨率
        score += min((w * h) / 1_000_000.0, 1.0) * 40
        if _has_qr(path):
            return None, "含二维码"
        return score, ("黑底" if avg < 60 else "ok")
    except Exception as e:
        return None, "异常: %s" % e


def select_topk(imgdir, k=6):
    """启发式从 imgdir 挑出评分最高的前 k 张图，返回 [(filename, score, reason), ...]"""
    if not os.path.isdir(imgdir):
        return []
    cands = []
    for fn in os.listdir(imgdir):
        if not fn.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
            continue
        if fn.startswith("_"):
            continue
        path = os.path.join(imgdir, fn)
        sc, reason = _image_score(path)
        if sc is None:
            continue
        cands.append((fn, sc, reason))
    cands.sort(key=lambda r: -r[1])
    return cands[:k]


def normalize_bg_to_bytes(in_path, force=False):
    """去背景 + 贴白底。返回 JPEG bytes；force=True 总是处理，否则已经很白就跳过。
    实现：rembg（u2net）成功用 rembg，否则回退到 PIL 提亮。"""
    from io import BytesIO
    try:
        im = load_rgb(in_path)
        # 检查是否已经够白
        thumb = im.copy(); thumb.thumbnail((40, 40))
        pixels = list(thumb.getdata())
        avg = sum(sum(p) for p in pixels) / (40 * 40 * 3)
        if not force and avg > 210:
            return None
        # 优先 rembg
        try:
            import rembg
            out = rembg.remove(im)  # returns bytes (PNG)
            rgba = Image.open(BytesIO(out)).convert("RGBA")
            bg = Image.new("RGB", rgba.size, "white")
            bg.paste(rgba, mask=rgba.split()[3])
            buf = BytesIO()
            bg.save(buf, format="JPEG", quality=92)
            return buf.getvalue()
        except Exception:
            # 回退：把图按一定阈值提亮（暗→更白）但保留高对比
            def lift(v):
                if v < 50: return 255
                if v < 80: return 245
                return min(255, int(v * 1.15 + 18))
            im2 = im.point(lift)
            buf = BytesIO()
            im2.save(buf, format="JPEG", quality=92)
            return buf.getvalue()
    except Exception:
        return None


# ---------------- AI 读懂产品（GLM-4V，机型专属卖点） ----------------
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
    """用 GLM-4V 看这台机器的真实官方图，提炼「图里看得见」的机型专属卖点，
    产出与 zhipu_ai9_copy 同 schema 的九张海报文案 JSON。
    相比纯文本的 zhipu_ai9_copy，本函数以真实产品图为依据，每台机器文案不同（非品类模板），
    即「AI 发挥到极致」的核心：逐台机器读懂 + 出题。
    硬约束不变：容量/能效/门数等参数取自 info，AI 只碰图里可见特征与软表达，不编数字。
    提示词里会带上 info["intro"]（第一步爬虫抓的官网产品介绍原文），让软卖点贴官方口径。
    结果缓存到 <src_dir>/_ai_read_v2.json。无 key / 失败 / 图太少 -> 返回 None（generate 回退）。"""
    if not api_key or not str(api_key).strip():
        return None
    p = Path(src_dir)
    cands = []
    for f in sorted(p.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
            continue
        try:
            im = Image.open(f); w, h = im.size
        except Exception:
            continue
        if w < 220 or h < 220:
            continue
        if w / h < 0.18 or w / h > 5.5:
            continue
        cands.append(f)
    if len(cands) < 2:
        return None
    # v2：提示词加入「官网产品介绍原文」后改版本号，让旧缓存失效、重新生成一次（只一次，之后照旧缓存）
    cache = p / "_ai_read_v2.json"
    try:
        if cache.exists():
            return json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        pass
    sheet = _build_contact_sheet(cands)
    data_url = "data:image/jpeg;base64," + base64.b64encode(sheet).decode("ascii")
    b = (info.get("brand") or "").strip()
    model = (info.get("model") or "").strip()
    cat = cat_name(info)
    door_txt = real_door(info)
    prompt = (
        "这是 %s %s（%s）的真实商品官方图拼版，每张左上角有红色编号。\n"
        "请只看【图里真实可见】的特征，提炼这台机器独有的可卖点，并生成 9 张闲鱼海报的文案 JSON。\n"
        "禁止编造参数；只写图里能确认的（如：双层抽屉、超薄机身、玻璃面板、触控屏、万向轮等）。\n"
        "真实门结构：%s（凡涉及「门」的文案都用它，禁止写不符的「三门/对开门/四门」）。\n"
        "%s"
        "只输出一个 JSON 对象（不要 markdown、不要解释）。字段：\n"
        '{"tag":"品类卖点短句8-14字",'
        '"o_soft":[["短标签","体验描述"],["短标签","体验描述"],["短标签","体验描述"]],'
        '"o_title":"容量/内部页大标题4-8字","o_sub":"副标题10-18字",'
        '"r_soft":[["",""],["",""],["",""]],'
        '"r_title":"尺寸/场景页大标题","r_sub":"副标题",'
        '"e_soft":[["",""],["",""],["",""]],'
        '"e_title":"能效页大标题","e_sub":"副标题",'
        '"d_soft":[["",""],["",""],["",""]],'
        '"d_title":"品质细节页大标题","m_soft":[["",""],["",""],["",""]],'
        '"angle_title":"立体角度页大标题","angle_sub":"副标题"}\n'
        "硬性要求：短标签2-5字、体验描述4-10字；禁止 emoji/特殊符号；只输出 JSON。"
        # 官网产品介绍原文：卖点措辞对齐官方口径；转义 % 以免原文里的百分号破坏格式化
    ) % (b, model, cat, (door_txt or "未知"), _intro_block(info).replace("%", "%%"))
    try:
        if log:
            try:
                log("正在用 AI 视觉读懂产品 ...")
            except Exception:
                pass
        txt = _zhipu_vision(api_key, data_url, prompt)
        obj = _parse_ai9_copy(txt)
        if obj:
            try:
                cache.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            return obj
    except Exception:
        pass
    return None


def norm_sev(v):
    """把视觉模型给的各种 severity 写法归一为「高 / 中 / 低」三档。"""
    s = str(v or "").strip().lower()
    if any(k in s for k in ("高", "严重", "high", "crit")):
        return "高"
    if any(k in s for k in ("中", "注意", "medium", "med", "warn")):
        return "中"
    if any(k in s for k in ("低", "轻微", "low", "minor")):
        return "低"
    return "低"


def _extract_json_array(text):
    """从模型回复里稳健地取出 JSON 数组（容忍 ```json 包裹、前后夹带解释文字）。"""
    s = (text or "").strip()
    if s.startswith("```"):
        try:
            s = s.split("```", 2)[1]
            s = s.lstrip()
            if s.lower().startswith("json"):
                s = s[4:]
        except Exception:
            pass
    try:
        return json.loads(s)
    except Exception:
        pass
    i, j = s.find("["), s.rfind("]")
    if 0 <= i < j:
        try:
            return json.loads(s[i:j + 1])
        except Exception:
            return None
    return None


def qa_render(api_key, image_paths, info=None, log=None):
    """用 GLM-4V 对生成的成片做视觉质检，返回问题列表（每项 {slot,issue,severity,code}）。
    code 为确定性布局类枚举（text_overlap/text_clip/product_clip）或需人工类（style_mismatch/broken/text_mismatch），
    空串表示模型未给、由消费方按 issue 反推。只保留「有具体问题描述」的条目。
    无 key / 失败 -> 返回 None；没问题 -> 返回 []。"""
    if not api_key or not str(api_key).strip():
        return None
    paths = [Path(x) for x in (image_paths or []) if Path(x).exists()]
    if not paths:
        return None
    sheet = _build_contact_sheet(paths, cols=3)
    data_url = "data:image/jpeg;base64," + base64.b64encode(sheet).decode("ascii")
    prompt = (
        "你是资深电商视觉审稿人。下面是同一商品为闲鱼生成的 %d 张海报拼版，"
        "每张左上角有红字编号（1 到 %d）。\n"
        "请逐张严格检查，并且【只为确实有问题的图输出条目】，没问题的图不要输出。\n"
        "检查项：\n"
        "1) 文字是否压住机器、文字互相重叠或被截断；\n"
        "2) 机器是否被画布边缘裁掉（缺边、缺角）；\n"
        "3) 图文是否相符（海报文字与该图内容矛盾）；\n"
        "4) 底色/风格是否与其它张明显不统一（出现白底或明显异色）；\n"
        "5) 是否明显破图（大面积空白、错位、马赛克）。\n"
        "输出严格为 JSON 数组（确实没问题时输出 []），每项四个字段：\n"
        '{"slot": 整数1到%d, "severity": "高"或"中"或"低", '
        '"code": "text_overlap(文字压机器/重叠) / text_clip(文字被截断溢出) / '
        'product_clip(机器被画布裁掉) / style_mismatch(底色风格不统一) / broken(明显破图) / '
        'text_mismatch(图文不符) 之一", "issue": "10-24字中文，具体到哪一处"}\n'
        "硬性要求：issue 必须具体可执行（例如「顶部标题压住机身」「机器左侧被裁掉」），"
        "禁止留空、禁止写「无」「正常」；severity 只能是 高 / 中 / 低 三个字之一；"
        "code 必须从上述 6 个枚举中选一个。\n"
        "只输出 JSON 数组，不要解释，不要 markdown 代码块。"
    ) % (len(paths), len(paths), len(paths))
    try:
        if log:
            try:
                log("正在 AI 成片自检 ...")
            except Exception:
                pass
        txt = _zhipu_vision(api_key, data_url, prompt)
        arr = _extract_json_array(txt)
        if not isinstance(arr, list):
            return None
        out = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            try:
                slot = int(it.get("slot"))
            except Exception:
                continue
            if slot < 1 or slot > len(paths):
                continue
            issue = str(it.get("issue") or "").strip().strip("。. ")
            if not issue or issue in ("无", "正常", "ok", "OK", "OK。", "-", "None", "none"):
                continue
            _code = str(it.get("code") or "").strip().lower()
            _codes = {"text_overlap", "text_clip", "product_clip",
                      "style_mismatch", "broken", "text_mismatch"}
            if _code not in _codes:
                _code = ""
            out.append({"slot": slot, "issue": issue,
                        "severity": norm_sev(it.get("severity")), "code": _code})
        return out
    except Exception:
        pass
    return None

