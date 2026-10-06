# -*- coding: utf-8 -*-
"""
按 _payload_kdocs.json 的型号列表补图：catalog.json（海尔/卡萨帝官网库）→ images_cache.json。

与 fetch_images.py 的区别：型号来源不是 xlsx，而是已解析好的在线表 payload；
并且会先做命中率体检（精确 core / 前缀 core / 完全无），再按需下载。

用法: python fetch_images_kdocs.py [--dry]
"""
import base64
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build import core_of, norm_model  # noqa: E402

LOG = os.path.join(HERE, "_fetch_kdocs.log")


def log(msg):
    print(msg)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(str(msg) + "\n")


def raw_get(url, referer, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Referer": referer,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def to_white_rgb(im):
    """透明底 PNG 合成到白底（直接 convert('RGB') 会把透明区填成黑）。"""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        return bg
    return im.convert("RGB")


def fetch_jpeg(url, referer, timeout=30):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
        "Accept": "image/*,*/*;q=0.8",
        "Referer": referer,
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    im = to_white_rgb(Image.open(io.BytesIO(raw)))
    im.thumbnail((220, 220))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=68)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def fetch_smart(rec, referer):
    """先试常规构造的图 URL；404 时抓详情页找真实图片地址。"""
    try:
        return fetch_jpeg(rec["img"], referer)
    except urllib.error.HTTPError as e:
        if e.code != 404 or not rec.get("u"):
            raise
        af = rec["img"].rsplit("/", 1)[-1]
        detail = rec["u"]
        if detail.startswith("//"):
            detail = "https:" + detail
        host = re.match(r"https?://[^/]+", detail).group(0)
        html = raw_get(detail, referer=host).decode("utf-8", "ignore")
        m = re.search(r'((?:https?:)?//[^"\'\s>]+/' + re.escape(af) + r')', html)
        if not m:
            raise
        u = m.group(1)
        if u.startswith("//"):
            u = "https:" + u
        return fetch_jpeg(u, referer=host)


def main():
    dry = "--dry" in sys.argv

    with io.open(os.path.join(HERE, "_payload_kdocs.json"), encoding="utf-8") as f:
        payload = json.load(f)
    rows = payload["rows"]
    cores = {}
    for r in rows:
        ck = r["c"]
        if ck and ck not in cores:
            cores[ck] = r["mo"]
    log("在线表型号 core: %d 个" % len(cores))

    cat_path = os.path.join(HERE, "catalog.json")
    cat = json.load(io.open(cat_path, encoding="utf-8")) if os.path.isfile(cat_path) else {}
    log("catalog: %d 个官网 core" % len(cat))

    cache_path = os.path.join(HERE, "images_cache.json")
    cache = json.load(io.open(cache_path, encoding="utf-8")) if os.path.isfile(cache_path) else {}
    log("images_cache: %d 张" % len(cache))

    def strip_paren(x):
        """剥掉括号及其内容 + 结尾的 套机/TC 之类业务后缀，拿到型号本体。"""
        t = re.sub(r"\([^)]*\)", "", x)
        t = re.sub(r"(套机|-套机|/TC|TC)$", "", t)
        return t.rstrip("-_/+.,")

    exact, prefix_key, none = [], [], []
    for ck, mo in sorted(cores.items()):
        if ck in cat:
            exact.append((ck, mo, ck))
            continue
        # ① 剥括号后精确命中官网库（JZT-xxx(12T) → JZT-xxx）
        sp = strip_paren(ck)
        if sp and sp != ck and sp in cat:
            prefix_key.append((ck, mo, sp))
            continue
        # ② 用本体做前缀互含
        base = sp or ck
        cands = [k for k in cat if (len(k) >= 6 and (base.startswith(k) or k.startswith(base)))]
        if cands:
            best = max(cands, key=len)
            prefix_key.append((ck, mo, best))
        else:
            none.append((ck, mo))

    log("--- 命中 ---")
    log("精确命中官网库: %d" % len(exact))
    log("前缀模糊命中  : %d" % len(prefix_key))
    log("官网库完全没谱: %d" % len(none))
    have = sum(1 for ck in cores if ck in cache)
    log("已有图: %d (%.1f%%)" % (have, 100.0 * have / max(len(cores), 1)))

    if dry:
        with io.open(os.path.join(HERE, "_miss_kdocs.txt"), "w", encoding="utf-8") as f:
            f.write("== 前缀模糊命中 ==\n")
            for ck, mo, k in prefix_key:
                f.write("%s\t%s\t->\t%s\n" % (ck, mo, k))
            f.write("\n== 官网库完全没谱 (%d) ==\n" % len(none))
            for ck, mo in none:
                f.write("%s\n" % mo)
        log("dry-run，清单 → _miss_kdocs.txt")
        return

    todo = []
    for ck, mo, key in exact:
        if ck not in cache:
            todo.append((ck, cat[key]))
    for ck, mo, key in prefix_key:
        if ck not in cache:
            todo.append((ck, cat[key]))
    log("待下载 %d 张" % len(todo))

    ok = fail = 0
    for i, (ck, rec) in enumerate(todo, 1):
        if not rec.get("img"):
            continue
        ref = "https://www.casarte.com/" if rec.get("b") == "卡萨帝" else "https://www.haier.com/cn/"
        try:
            cache[ck] = "data:image/jpeg;base64," + fetch_smart(rec, ref)
            ok += 1
        except Exception as e:
            fail += 1
            log("  fail %s: %r" % (ck, e))
        if i % 20 == 0:
            log("  %d/%d ok=%d fail=%d" % (i, len(todo), ok, fail))
            with io.open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f)
        time.sleep(0.12)

    with io.open(cache_path, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    have2 = sum(1 for ck in cores if ck in cache)
    log("=== done ok=%d fail=%d → 有图 %d (%.1f%%) ===" % (
        ok, fail, have2, 100.0 * have2 / max(len(cores), 1)))


if __name__ == "__main__":
    main()
