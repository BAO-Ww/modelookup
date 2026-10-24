# -*- coding: utf-8 -*-
"""
卡萨帝补图/换图 → 华克斯电器商城 whycools.cn（2026-10-24 用户指定图源）。

目标（满足其一）：
  ① 本地 payload 没图的卡萨帝型号（门体/面板/遥控器类自动跳过）
  ② 云端 myimgs 被管理号隐藏的（b=='hide'，即「图不对先隐藏」的卡）
  ③ 命令行强指的型号（图不对要换的）：python fetch_whycools.py C6SO46BGU1 ...

命中判定（按卡标题里的 [A-Z0-9]{5,} token）：
  ① token == 型号骨架（完全相等） ② 型号骨架以 token 开头/结尾且 token≥6 位
  ③ 同长度且 ≤2 字差异（C6SO46BGU1 ↔ C3SO46BGU1）且为唯一最优
命中后取详情页第一张 picproduct/<stamp>.jpg 大图 → 垫白缩 240 → JPEG q70 dataURL
→ 存 _whycools_imgs.json + curl POST /myimg 上云（覆盖隐藏/错图，人人可见）。

用法: python fetch_whycools.py [--dry] [型号|卡片key ...]
"""
import base64
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
PAYLOAD = r"C:/Users/Administrator/AppData/Local/ModelLookup/payload.json"
TOKD = r"C:/Users/Administrator/AppData/Local/ModelLookup/auth_token.json"
HOST = "https://auth.tingjiabaobao.de5.net"
OUT = os.path.join(HERE, "_whycools_imgs.json")
LOG = os.path.join(HERE, "_whycools.log")
socket.setdefaulttimeout(25)

# 直连（国内站，不走 Clash）
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(m):
    print(m)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(str(m) + "\n")


def alnum(s):
    return re.sub(r"[^0-9A-Z]", "", str(s or "").upper())


def strip_paren(x):
    return re.sub(r"[（(][^）)]*[）)]", "", str(x or ""))


def get(u):
    r = OP.open(urllib.request.Request(u, headers={
        "User-Agent": UA, "Referer": "https://www.whycools.cn/",
        "Accept": "text/html,image/*,*/*", "Accept-Language": "zh-CN,zh;q=0.9"}))
    return r.read()


def search(kw):
    """返回 [{link,title,img}]；keyword= 才是真的搜索参数（key= 会被忽略）。"""
    h = get("https://www.whycools.cn/search.aspx?keyword=" + urllib.parse.quote(kw))
    h = h.decode("utf-8", "ignore")
    cards = []
    for chunk in h.split("<!--product item-->")[1:]:
        m = re.search(r'href="(/product/[^"]+\.html)"', chunk)
        if not m:
            continue
        t = re.search(r"<h5[^>]*>\s*([^<]+?)\s*</h5>", chunk)
        im = re.search(r'src="([^"]+picproduct[^"]+)"', chunk)
        cards.append({
            "link": "https://www.whycools.cn" + m.group(1),
            "title": (t.group(1).strip() if t else ""),
            "img": im.group(1) if im else "",
        })
    return cards


def hamming(a, b):
    return sum(1 for x, y in zip(a, b) if x != y)


def pick(mo, cards):
    """返回 (card, why)；命中不了返回 (None, no)"""
    t = alnum(mo)
    if len(t) < 5:
        return None, "short"
    best, best_score, ties = None, None, 0
    for cd in cards:
        title = cd.get("title") or ""
        ta = alnum(title)
        toks = [x for x in re.findall(r"[A-Z0-9]{5,}", title.upper())]
        score = None
        if ta == t:
            score = 0
        else:
            for tok in toks:
                if tok == t:
                    score = 0
                    break
                if len(tok) >= 6 and len(tok) >= len(t) - 4 and (t.startswith(tok) or t.endswith(tok)):
                    score = min(score if score is not None else 9, 1)
            if score is None and len(ta) >= 8 and (t in ta or ta in t):
                score = 2
            if score is None:
                for tok in toks:
                    if len(tok) == len(t):
                        d = hamming(tok, t)
                        if d <= 2:
                            score = min(score if score is not None else 99, 10 + d)
        if score is None:
            continue
        if best_score is None or score < best_score:
            best, best_score, ties = cd, score, 1
        elif score == best_score:
            ties += 1
    if best is None:
        return None, "no"
    if best_score >= 10 and ties > 1:
        return None, "ambiguous"
    return best, {0: "eq", 1: "affix", 2: "contain"}.get(best_score, "fuzzy%d" % (best_score - 10))


def detail_imgs(link):
    """详情页大图（按出现顺序去重，跳过 240 缩略路径）"""
    h = get(link).decode("utf-8", "ignore")
    seen, out = set(), []
    for m in re.finditer(r'(?:src|href)="([^"]*picproduct/[^"]+\.jpg)"', h):
        u = m.group(1)
        if "/240/" in u or u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out


def to_white_rgb(im):
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[3])
        return bg
    return im.convert("RGB")


def to_dataurl(raw):
    im = to_white_rgb(Image.open(io.BytesIO(raw)))
    im.thumbnail((240, 240))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=70)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def cloud_push(c, b64, tok):
    """curl 上传（python urllib 大 POST 会被代理断连，沿用 _imgs_cloud_push 的教训）"""
    body = json.dumps({"c": c, "b64": b64})
    io.open(os.path.join(HERE, "_why_chunk.json"), "w", encoding="utf-8").write(body)
    r = subprocess.run([
        "curl", "-s", "-m", "60", "-X", "POST",
        "-H", "X-Token: " + tok, "-H", "Content-Type: application/json",
        "--data-binary", "@_why_chunk.json", HOST + "/myimg"
    ], capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(r.stdout).get("ok")
    except Exception:
        return False


SKIP = re.compile(r"门体|面板|遥控器|把手|套系|延保|装饰|支架|配件|附件|碗柜|配B29|配CWC")


def main():
    dry = "--dry" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--dry"]
    tok = json.load(io.open(TOKD, encoding="utf-8")).get("token", "")
    p = json.load(io.open(PAYLOAD, encoding="utf-8"))
    rows = p["rows"]
    imgs = p.get("imgs") or {}

    # 云端 myimgs 现状（隐藏标记也算缺图目标）
    cloud = {}
    if tok:
        try:
            r = OP.open(urllib.request.Request(HOST + "/myimgs", headers={
                "X-Token": tok, "User-Agent": UA, "Accept": "application/json"}), timeout=25)
            cloud = (json.loads(r.read().decode("utf-8", "ignore")).get("imgs") or {})
        except Exception as e:
            log("⚠ 拉云端 myimgs 失败（%s）——按本地口径跑" % e)

    cas = {}
    for r in rows:
        if r.get("b") != "卡萨帝":
            continue
        c = r["c"]
        if c not in cas:
            cas[c] = r

    forced = set()
    for a in args:
        na = alnum(a)
        hit = [c for c in cas if c == a or alnum(c) == na or alnum(cas[c].get("mo")) == na]
        if hit:
            forced.update(hit)
        else:
            log("⚠ 命令行给的 [%s] 不在卡萨帝表里，忽略" % a)

    todo = []
    for c, r in cas.items():
        nm = (r.get("n") or "") + (r.get("mo") or "")
        if SKIP.search(nm):
            continue
        ce = cloud.get(c) or {}
        has_img = c in imgs or str(ce.get("b", "")).startswith("data:image")
        hidden = ce.get("b") == "hide"
        if c in forced or hidden or not has_img:
            todo.append((c, r.get("mo"), r.get("n") or ""))
    log("=== 待补 %d 个（强指 %d）===" % (len(todo), len(forced)))
    if dry:
        for x in todo:
            log("  %s %s %s" % x)
        return

    out = json.load(io.open(OUT, encoding="utf-8")) if os.path.isfile(OUT) else {}
    ok = fail = 0
    for i, (c, mo, name) in enumerate(todo, 1):
        variants, vseen = [], set()
        for v in (mo, re.sub(r"\s+", "", str(mo or "")), strip_paren(mo), c, name):
            v = (v or "").strip()
            if v and v not in vseen:
                vseen.add(v)
                variants.append(v)
        cd, why, chosen = None, "no_match", None
        for v in variants:
            try:
                pl = search(v)
            except Exception as e:
                why = "search:" + repr(e)[:40]
                continue
            cd, why = pick(mo, pl)
            if cd:
                break
            time.sleep(0.15)
        if not cd:
            fail += 1
            log("  ✗ %s（%s）" % (mo, why))
        else:
            try:
                us = detail_imgs(cd["link"])
                if not us and cd.get("img"):
                    us = [cd["img"].replace("/240/240", "/")]  # 缩略兜底
                if not us:
                    raise RuntimeError("详情页无图")
                chosen = us[0]
                d = to_dataurl(get(chosen))
                out[c] = d
                if cloud_push(c, d, tok):
                    ok += 1
                    log("  ok %s ← %s（%s，%d 图取第1）" % (mo, cd["title"], why, len(us)))
                else:
                    fail += 1
                    log("  ✗ %s 图抓到了但上云失败（已存 %s）" % (mo, os.path.basename(OUT)))
            except Exception as e:
                fail += 1
                log("  ✗ %s 图处理失败（%s）" % (mo, repr(e)[:60]))
        if i % 5 == 0:
            json.dump(out, io.open(OUT, "w", encoding="utf-8"))
        time.sleep(0.2)
    json.dump(out, io.open(OUT, "w", encoding="utf-8"))
    log("=== whycools 补图 ok=%d fail=%d；输出 %s ===" % (ok, fail, os.path.basename(OUT)))


if __name__ == "__main__":
    main()
