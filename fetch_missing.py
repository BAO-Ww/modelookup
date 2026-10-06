# -*- coding: utf-8 -*-
"""
按 _payload_kdocs.json 的缺图型号 → 海尔商城 s.ehaier.com 补图。

命中判定：productSecondName 的字母数字骨架 与 型号骨架（含剥括号版）相等 或 长度>=6 前缀互含。
输出 _missing_imgs.json，跑完自动并入 images_cache.json。

用法: python fetch_missing.py [--dry]
"""
import base64
import io
import json
import os
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
OUT = os.path.join(HERE, "_missing_imgs.json")
LOG = os.path.join(HERE, "_missing.log")
CACHE = os.path.join(HERE, "images_cache.json")
socket.setdefaulttimeout(25)

API = ("https://s.ehaier.com/search/wdCommonSearchNew.html"
       "?provinceId=16&cityId=173&districtId=2450&streetId=12036596&fromType=1"
       "&visitStore=&memberId=&keyword=%s&qs=isHotDesc&filterData=all%%40all%%400"
       "&pageIndex=1&pageSize=20&actTagId=&productCateId=&longitude=120.41803"
       "&latitude=36.12784&visitMdCode=")


def log(m):
    print(m)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(str(m) + "\n")


def alnum(s):
    return re.sub(r"[^0-9A-Z]", "", str(s or "").upper())


def strip_paren(x):
    return re.sub(r"\([^)]*\)", "", str(x or ""))


def call(kw):
    req = urllib.request.Request(API % urllib.parse.quote(kw), headers={
        "User-Agent": UA, "Referer": "https://www.ehaier.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def to_white_rgb(im):
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        return bg
    return im.convert("RGB")


def download_thumb(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://www.ehaier.com/", "Accept": "image/*,*/*"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    im = to_white_rgb(Image.open(io.BytesIO(raw)))
    im.thumbnail((240, 240))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def pick(core, pl):
    vset = set()
    for v in (alnum(core), alnum(strip_paren(core))):
        if len(v) >= 4:
            vset.add(v)
    for p in pl:
        p2 = alnum(p.get("productSecondName"))
        if p2 and p2 in vset:
            return p, "eq"
    for p in pl:
        p2 = alnum(p.get("productSecondName"))
        if not p2 or len(p2) < 6:
            continue
        for v in vset:
            if len(v) >= 6 and (p2.startswith(v) or v.startswith(p2)):
                return p, "prefix"
    return None, "no"


def main():
    dry = "--dry" in sys.argv
    with io.open(os.path.join(HERE, "_payload_kdocs.json"), encoding="utf-8") as f:
        payload = json.load(f)
    rows = payload["rows"]
    imgs = payload.get("imgs") or {}

    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.isfile(CACHE) else {}
    out = json.load(io.open(OUT, encoding="utf-8")) if os.path.isfile(OUT) else {}

    todo, seen = [], set()
    for r in rows:
        ck = r["c"]
        if ck in seen:
            continue
        seen.add(ck)
        if ck in imgs or ck in cache or out.get(ck):
            continue
        # 配件 / 门体 类没有整机图，跳过省时间
        nm = r.get("n") or ""
        mo = r.get("mo") or ""
        if re.search(r"门体|套系|把手|面板|支架|配件|装饰|延保|套机", nm + mo):
            continue
        todo.append((ck, mo, nm))
    log("=== 待补 %d 个（已跳过配件/门体类）===" % len(todo))
    if dry:
        return

    ok = fail = 0
    for i, (ck, mo, name) in enumerate(todo, 1):
        variants, vseen = [], set()
        for v in (mo, re.sub(r"\s+", "", str(mo or "")), strip_paren(mo), ck, name):
            v = (v or "").strip()
            if v and v not in vseen:
                vseen.add(v)
                variants.append(v)
        p = None
        why = "no_match"
        for v in variants:
            try:
                d = call(v)
                pl = (d.get("data") or {}).get("productList") or []
            except Exception as e:
                why = "api:" + repr(e)[:40]
                continue
            p, why = pick(ck, pl)
            if p:
                break
            time.sleep(0.1)
        if p:
            u = p.get("defaultImageUrl") or ""
            if u:
                try:
                    out[ck] = download_thumb(u)
                    ok += 1
                    log("  ok %s ← %s" % (mo, p.get("productSecondName")))
                except Exception as e:
                    fail += 1
                    why = "img:" + repr(e)[:40]
            else:
                fail += 1
        else:
            fail += 1
        if i % 10 == 0:
            log("  %d/%d ok=%d fail=%d" % (i, len(todo), ok, fail))
            json.dump(out, io.open(OUT, "w", encoding="utf-8"))
        time.sleep(0.18)

    out = {k: v for k, v in out.items() if v}
    json.dump(out, io.open(OUT, "w", encoding="utf-8"))
    log("=== ehaier 补图 ok=%d fail=%d ===" % (ok, fail))

    # 并入主缓存
    n = 0
    for k, v in out.items():
        if isinstance(v, str) and v.startswith("data:image/") and k not in cache:
            cache[k] = v
            n += 1
    json.dump(cache, io.open(CACHE, "w", encoding="utf-8"))
    have = sum(1 for r in rows if r["c"] in cache)
    log("并入 %d 张 → images_cache %d 张；本表有图 %d/%d (%.1f%%)" % (
        n, len(cache), have, len(seen), 100.0 * have / max(len(seen), 1)))


if __name__ == "__main__":
    main()
