# -*- coding: utf-8 -*-
"""兴达商城（xdjdy.com）渠道源：官网三站查不到的型号从这里兜底。
商品图多为 file.c.haier.net 官方素材原图（与官网同源、无水印）。
索引：xdjdy_index.json {gid: title}，由 data/build_xdjdy_index.py 生成。"""
import os, re, json, ssl, time
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor
from net import urlopen_ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0 Safari/537.36"
HOST = "https://www.xdjdy.com"

_idx_cache = {}


def _index_paths():
    """索引候选路径：打包资源目录 -> 源码目录 -> 仓库 data 目录"""
    import sys
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    here = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(base, "assets", "pei", "xdjdy_index.json"),
            os.path.join(here, "assets", "pei", "xdjdy_index.json"),
            os.path.join(here, "xdjdy_index.json"),
            os.path.normpath(os.path.join(here, "..", "data", "xdjdy_index.json"))]


def load_index():
    if "idx" in _idx_cache:
        return _idx_cache["idx"]
    idx = {}
    for p in _index_paths():
        if os.path.exists(p):
            try:
                idx = json.load(open(p, encoding="utf-8"))
                break
            except Exception:
                continue
    _idx_cache["idx"] = idx
    return idx


def _norm(s):
    return str(s).upper().replace(" ", "")


_MODEL_RE = re.compile(r"[A-Za-z]{2,4}-?\d{1,4}[A-Za-z0-9\-]*")


def _title_model(title):
    cands = _MODEL_RE.findall(str(title))
    return max(cands, key=len).upper() if cands else ""


def find_xdjdy(model, log=None):
    """型号 -> 商品页 URL。本地索引匹配 -> 在线搜索兜底。未命中返回 None。"""
    key = _norm(model)
    idx = load_index()
    if idx:
        if log:
            log("查兴达商城本地库（%d 款）..." % len(idx))
        hits = []
        for gid, t in idx.items():
            if not t or not t.strip():
                continue
            tt = _norm(t)
            if key in tt:
                hits.append((10000 + len(tt) - len(key), gid, t))
        if not hits:
            for gid, t in idx.items():
                if not t or not t.strip():
                    continue
                tm = _title_model(t)
                if not tm:
                    continue
                # 包含关系（短边>=5字符）视为命中，与官网索引同标准
                lo, hi = (key, tm) if len(key) <= len(tm) else (tm, key)
                if len(lo) >= 5 and lo in hi:
                    hits.append((5000, gid, t))
        if hits:
            hits.sort(key=lambda x: (x[0], len(x[2])), reverse=True)
            score, gid, t = hits[0]
            if log:
                log("  兴达商城命中：goods-%s" % gid)
            return "%s/goods-%s.html" % (HOST, gid)
    if log:
        log("兴达商城本地库未命中，在线搜索 ...")
    try:
        q = urllib.parse.quote(str(model).strip())
        req = urllib.request.Request(
            HOST + "/index.php?w=search&t=search&keyword=" + q,
            headers={"User-Agent": UA})
        h = urlopen_ssl(req, timeout=25).read().decode("utf-8", "ignore")
        best = None
        for gid, t in re.findall(r'href="[^"]*goods-(\d+)\.html"[^>]*title="([^"]*)"', h):
            if not t.strip():
                continue
            if key in _norm(t):
                if not best or len(t) < len(best[1]):
                    best = (gid, t)
        if best:
            if log:
                log("  在线搜索命中：goods-%s" % best[0])
            return "%s/goods-%s.html" % (HOST, best[0])
    except Exception:
        pass
    return None


def http(url, retry=2):
    for i in range(retry):
        try:
            # haier.net 图需要 haier referer；站内图不挑 referer
            ref = "https://www.haier.com/" if "haier.net" in url else HOST + "/"
            return urlopen_ssl(
                urllib.request.Request(url, headers={"User-Agent": UA, "Referer": ref}),
                timeout=30).read()
        except Exception as e:
            if i == retry - 1:
                raise
            time.sleep(1.5)
    return None


def _brand_of(title):
    t = str(title)
    if "卡萨帝" in t or "CASARTE" in t.upper():
        return "卡萨帝"
    if "统帅" in t or "LEADER" in t.upper():
        return "统帅"
    return "海尔"


def _props_from_title(title):
    """无结构化参数表，从标题提取规格/尺寸"""
    props = []
    m = re.search(r"\d+(?:\.\d+)?\s*(?:公斤|升|匹)", title)
    if m:
        props.append(("规格", m.group(0).replace(" ", "")))
    m = re.search(r"(\d{3,4}x\d{3,4}x\d{3,4})\s*(?:mm)?", title)
    if m:
        props.append(("外形尺寸", m.group(1) + "mm"))
    return props


def parse_xdjdy_page(html, url):
    """解析商品页：标题、价格、型号、图片（官方原图优先 + 站内大图兜底）"""
    m = re.search(r"<h1[^>]*>([^<]{6,160})</h1>", html)
    title = m.group(1).strip() if m else ""
    if not title:
        m = re.search(r"<title>([^<]+)</title>", html, re.I)
        title = re.sub(r"\s*[-–]\s*兴达商城\s*$", "", m.group(1)).strip() if m else ""
    # 价格：商城价 / goods_price
    price = 0
    for pat in [r"商城价[^0-9]{0,30}([0-9]{3,7}(?:\.[0-9]{1,2})?)",
                r"goods_price[^0-9]{0,10}([0-9]{3,7}(?:\.[0-9]{1,2})?)"]:
        m = re.search(pat, html)
        if m:
            try:
                price = int(float(m.group(1)))
                break
            except Exception:
                pass
    # 型号：先试 model=xxx 属性，再从标题抠
    model = ""
    m = re.search(r'(?:model|goodsCode|itemCode)["\s]*[:=]["\s]*([A-Za-z0-9\- ]{5,30})["\s>]', html)
    if m:
        model = m.group(1).strip().upper()
    if not model or len(model) < 5:
        for pat in [r"[A-Z]{2,6}[ \-]?[A-Z0-9]{5,20}(?= |，|,|-|·)",
                    r"[A-Z]{2,6}[0-9A-Z\-]{6,20}"]:
            m = re.search(pat, title.upper())
            if m:
                model = m.group(0).replace(" ", "")
                break
    if not model:
        m = re.search(r"goods-(\d+)\.html", url)
        model = "XDJDY-" + (m.group(1) if m else "UNKN")

    # 1) 官方原图（haier.net 素材库，无水印）
    order = []
    for m in re.finditer(
            r'(?:src|data-src|data-original)="([^"]*(?:haier\.net|haier\.com)[^"]+\.(?:jpg|jpeg|png|webp))"',
            html, re.I):
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        if u not in order:
            order.append(u)
    n_official = len(order)
    # 2) 站内商品大图（缩略图 _360/_60 升级到 _1280）
    seen_id = set()
    n_store = 0
    for m in re.finditer(
            r'(?:src|data-src|data-original)="([^"]*upfiles/shop/store/goods/[^"]+_\d+\.(?:jpg|jpeg|png|webp))"',
            html, re.I):
        u = re.sub(r"_(360|60|180|240)\.(jpg|jpeg|png|webp)$", r"_1280.\2",
                   m.group(1), flags=re.I)
        u = u.replace("_1280.jpg.jpg", "_1280.jpg")
        mid = re.search(r"(\w+)_\d+\.", u.split("/")[-1])
        if mid and mid.group(1) in seen_id:
            continue
        if mid:
            seen_id.add(mid.group(1))
        if u not in order:
            order.append(u)
            n_store += 1

    return {
        "site": "xdjdy",
        "brand": _brand_of(title),
        "model": model,
        "title": title,
        "price": price,
        "avail": "",
        "props": _props_from_title(title),
        "url": url,
        "images": order,
        "_n_official": n_official,
        "_n_store": n_store,
    }


def download_images(info, outdir, progress_callback=None):
    os.makedirs(outdir, exist_ok=True)
    jobs = []
    for i, u in enumerate(info["images"]):
        ext = os.path.splitext(u.split("?")[0])[1] or ".jpg"
        jobs.append((u, os.path.join(outdir, "%02d%s" % (i + 1, ext))))

    def dl(a):
        u, path = a
        try:
            d = http(u)
        except Exception:
            return 0
        if d and len(d) > 2048:
            with open(path, "wb") as f:
                f.write(d)
            return 1
        return 0

    ok = 0
    n = max(1, len(jobs))
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(dl, jobs):
            ok += r
            if progress_callback:
                progress_callback(15 + 80 * ok // n, "下载图片 %d/%d" % (ok, n))
    return ok


def guess_cat(title):
    for k, v in [("冰箱", "冰箱"), ("冷柜", "冷柜"), ("洗衣", "洗衣机"), ("空调", "空调"),
                 ("热水器", "热水器"), ("电视", "电视"), ("洗碗", "厨电"), ("油烟", "厨电"),
                 ("燃气灶", "厨电"), ("酒柜", "酒柜"), ("冰吧", "冰吧")]:
        if k in title:
            return v
    return "电器"


def fetch_xdjdy(url, outdir, log=None, progress=None):
    """抓渠道商城商品页并下载图片，返回 info（与 core.fetch_product 同构）"""
    def cb(p, m):
        if progress:
            progress(p, m)
    cb(0, "抓取兴达商城商品页 ...")
    try:
        data = http(url)
    except Exception:
        data = None
    if not data:
        cb(100, "抓取失败")
        return None
    html = data.decode("utf-8", "ignore")
    info = parse_xdjdy_page(html, url)
    if not info["images"]:
        cb(100, "页面没有可用图片")
        return None
    if log:
        log("图片共 %d 张（官方原图 %d + 站内大图 %d）" % (
            len(info["images"]), info["_n_official"], info["_n_store"]))
    cb(15, "开始下载图片 ...")
    ok = download_images(info, outdir, cb)
    if log:
        log("图片下载 %d/%d 张" % (ok, len(info["images"])))
    if ok == 0:
        cb(100, "图片全部下载失败")
        return None
    info.pop("_n_official", None)
    info.pop("_n_store", None)
    info["dir"] = outdir
    info["cat"] = guess_cat(info["title"])
    json.dump(info, open(os.path.join(outdir, "info.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    cb(100, "完成")
    return info
