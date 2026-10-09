# -*- coding: utf-8 -*-
"""云端金山表格拉取器（GitHub Actions 定时任务跑，2026-10-09）：
Playwright 起 Chromium → 打开两个金山分享页（公布版 + 价格表，公开分享匿名可读，
跟 app/main.py 同一招：页面就绪后在 kdocs 域里发同源 XHR 拿签名直链）→
下载两个 xlsx → ts = 两个文件内容的 sha256（表格没改 ts 就不变，机器不会被无谓打扰）
→ 经 worker /xlsxcloud 上传（所有机器的软件 60 秒内自动跟随重建）。

凭据：CLOUD_TOKEN（管理号登录态，GitHub Secrets 提供）；
备用：KX_SECRET（worker env.SECRET，X-Cloud-Key 头也能过 /xlsxcloud 的鉴权）。
"""

import base64
import hashlib
import io
import json
import os
import time
import urllib.request

OUT = io.open(os.environ.get("GITHUB_STEP_SUMMARY") or os.devnull, "a", encoding="utf-8")


def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    OUT.write(str(s) + "\n")
    OUT.flush()


CLOUD = "https://auth.baozang.kdns.fr"
TOKEN = (os.environ.get("CLOUD_TOKEN") or "").strip()
SECRET = (os.environ.get("KX_SECRET") or "").strip()
if not TOKEN and not SECRET:
    raise SystemExit("CLOUD_TOKEN / KX_SECRET 至少要配一个")

TABLES = [
    {"name": "data.xlsx",  "share": "https://www.kdocs.cn/l/ch7gwGTP9De4",
     "fid": "548020345856"},   # 公布版-滞销样机清理明细
    {"name": "price.xlsx", "share": "https://www.kdocs.cn/l/cficbK9Drrdp",
     "fid": "570174983918"},   # 价格表
]

# 与 app/main.py sync_xhr_js 同一招：在 kdocs 域页面里发同源 XHR，自动带上会话 cookie
XHR_JS = ("(function(){try{"
          "var x=new XMLHttpRequest();"
          "x.open('GET','/api/v3/office/file/%s/download',false);"
          "x.send();"
          "return x.status+'|'+String(x.responseText);"
          "}catch(e){return 'ERR|'+e.message;}})()")


def worker_req(path, method="GET", obj=None, timeout=120):
    """调 worker：优先 CLOUD_TOKEN（管理号登录态），失败再试 X-Cloud-Key。"""
    body = json.dumps(obj).encode("utf-8") if obj is not None else None
    last = "no auth"
    for headers in (
        {"X-Token": TOKEN} if TOKEN else [],
        {"X-Cloud-Key": SECRET} if SECRET else [],
    ):
        req = urllib.request.Request(CLOUD + path, data=body, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "modelookup-cloud/1.0")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except urllib.error.HTTPError as e:
            txt = e.read().decode("utf-8", "ignore")[:200]
            last = "HTTP %s: %s" % (e.code, txt)
            if e.code in (401, 403):
                continue          # 这个凭据不行，换下一个
            raise RuntimeError(last)
        except Exception as e:
            last = repr(e)
    raise RuntimeError("worker 请求失败：%s %s" % (path, last))


def fetch_xlsx(page, ctx, tab):
    """打开分享页 → 等匿名会话就绪 → 同源 XHR 拿直链 → 下载 xlsx 字节。"""
    name, share, fid = tab["name"], tab["share"], tab["fid"]
    page.goto(share, wait_until="domcontentloaded", timeout=90000)
    raw = b""
    for attempt in range(6):          # app 也是先静默等十几秒，匿名会话往往就能拿直链
        page.wait_for_timeout(5000)
        r = page.evaluate(XHR_JS % fid)
        st, _, txt = str(r).partition("|")
        if st == "200" and txt[:2] != "PK":
            try:
                d = json.loads(txt)
            except Exception:
                d = {}
            du = d.get("download_url") or d.get("url") or ""
            if isinstance(du, str) and du.startswith("http"):
                resp = ctx.request.get(du, timeout=120000)
                raw = resp.body()
                if raw[:2] == b"PK":
                    log("%s：拿到直链并下载 %.2f MB（第 %d 次尝试）"
                        % (name, len(raw) / 1048576.0, attempt + 1))
                    return raw
                log("%s：直链返回的不是文件（前 4 字节 %r）" % (name, raw[:4]))
        else:
            log("%s：第 %d 次还没拿到（status=%s，%s）"
                % (name, attempt + 1, st, txt[:80].replace("\n", " ")))
    raise RuntimeError("%s：多次尝试都拿不到直链（分享页可能改了权限）" % name)


def main():
    from playwright.sync_api import sync_playwright

    # 云端现有 ts：一样就整单跳过（省下载、省上传、机器不被打扰）
    try:
        meta = worker_req("/xlsxmeta", timeout=60)
        old_ts = str(meta.get("ts") or "")
    except Exception as e:
        old_ts = ""
        log("探测云端 ts 失败（按无旧数据处理）：%s" % e)

    files, hashes = {}, []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="zh-CN", timezone_id="Asia/Shanghai")
        page = ctx.new_page()
        for tab in TABLES:
            raw = fetch_xlsx(page, ctx, tab)
            files[tab["name"]] = base64.b64encode(raw).decode("ascii")
            hashes.append(hashlib.sha256(raw).hexdigest()[:12])
        browser.close()

    ts = "-".join(hashes)
    log("本次内容哈希 ts=%s（云端 %s）" % (ts, old_ts or "无"))
    if ts == old_ts:
        log("两张表内容都没变，跳过上传（机器不会有任何感知）✔")
        return

    j = worker_req("/xlsxcloud", "POST",
                   {"ts": ts, "files": files}, timeout=300)
    if not j.get("ok"):
        raise RuntimeError("上传被拒：%s" % j.get("msg"))
    log("已上传云端（%.1f MB）——所有机器的软件 60 秒内自动跟随重建 ✔"
        % (j.get("mb") or 0))


if __name__ == "__main__":
    time.sleep(0)
    main()
