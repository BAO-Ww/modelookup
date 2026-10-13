# -*- coding: utf-8 -*-
"""手机端真软件化（2026-10-13）：CI 打 APK 前把「界面+数据」烤进 www/
① www/index.html ← 云端 /m（KV mhtml，公开 GET；比仓库 cloud/m.html 更贴近线上版）
② www/payload.js  ← 云端 /mpayload（公开 GET）包装成 window.__MP__ = {...}
   m.html 原生支持 window.__MP__：本地快照秒开首屏，后台再拉云端热更新（离线可查）。
产物校验：index.html 必须含脚本、rows 必须够多，防烤出一个空壳 APK。"""
import io
import json
import os
import ssl
import sys
import urllib.request

BASE = "https://auth.baozang.kdns.fr"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")   # 裸 UA 会被 CF 1010 拦


def fetch(path, timeout=180):
    h = {"User-Agent": UA}
    secret = (os.environ.get("KX_SECRET") or "").strip()      # worker env.SECRET → X-Cloud-Key
    token = (os.environ.get("CLOUD_TOKEN") or "").strip()     # 管理号登录态 → X-Token
    if secret:
        h["X-Cloud-Key"] = secret
    elif token:
        h["X-Token"] = token
    req = urllib.request.Request(BASE + path, headers=h)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(req, context=ctx, timeout=timeout).read()


def main():
    www = sys.argv[1] if len(sys.argv) > 1 else "www"
    os.makedirs(www, exist_ok=True)

    # ① 界面（+ 在主脚本前注入 payload.js 引用：APK 里加载本地快照；云端页不注入保持干净）
    html = fetch("/m").decode("utf-8", errors="strict")
    if "mpayload" not in html or len(html) < 20000:
        sys.exit("bake: /m 内容不对（%d bytes），拒收" % len(html))
    if "<script src=\"payload.js\">" not in html:
        html = html.replace("<script>", "<script src=\"payload.js\"></script>\n<script>", 1)
    io.open(os.path.join(www, "index.html"), "w", encoding="utf-8", newline="\n").write(html)
    print("bake: index.html %.1f KB" % (len(html.encode("utf-8")) / 1024.0))

    # ② 数据快照（JSON 原文直接拼成 JS 赋值，不再解析再序列化，保证字节级一致）
    raw = fetch("/mpayload").decode("utf-8", errors="strict")
    obj = json.loads(raw)
    rows = obj.get("rows") or []
    if len(rows) < 100:
        sys.exit("bake: /mpayload 只有 %d 行，像坏数据，拒收" % len(rows))
    io.open(os.path.join(www, "payload.js"), "w", encoding="utf-8", newline="\n").write(
        "/* CI 烤入的本地数据快照（发布该 APK 时的云端最新）；运行时会自动拉云端热更新 */\n"
        "window.__MP__ = " + raw + ";\n")
    meta = obj.get("meta") or {}
    print("bake: payload.js %.1f MB, rows=%d, built=%s"
          % (len(raw.encode("utf-8")) / 1048576.0, len(rows), meta.get("built", "?")))


if __name__ == "__main__":
    main()
