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


CLOUD = "https://auth.tingjiabaobao.de5.net"
TOKEN = (os.environ.get("CLOUD_TOKEN") or "").strip()
SECRET = (os.environ.get("KX_SECRET") or "").strip()
if not TOKEN and not SECRET:
    raise SystemExit("CLOUD_TOKEN / KX_SECRET 至少要配一个")

TABLES = [
    {"name": "data.xlsx",  "share": "https://www.kdocs.cn/l/ch7gwGTP9De4",
     "fid": "548020345856"},   # 公布版-滞销样机清理明细
    {"name": "price.xlsx", "share": "https://www.kdocs.cn/l/cficbK9Drrdp",
     "fid": "570174983918"},   # 价格表
    {"name": "subsidy.xlsx", "share": "https://www.kdocs.cn/l/cid8gbTzhB4c",
     "fid": "0"},              # 深圳以旧换新补贴表（要登录态；fileId 靠页面请求现查）
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
    """打开分享页 → 从页面网络请求里反查 fileId（软件 probe_file_id 同款，
    写死的 fid 可能过期）→ 同源 XHR 拿直链 → 下载 xlsx 字节。"""
    name, share, fid = tab["name"], tab["share"], tab["fid"]
    seen = []
    page.on("request", lambda r: seen.append(r.url))
    page.goto(share, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(8000)      # 等分享页自己发 API 请求，好从中捞真实 fileId
    # 诊断：落地到哪了、页面标题是什么（分享被限制时会跳登录页，一眼看出来）
    log("%s：落地 %s" % (name, (page.url or "")[:90]))
    try:
        log("%s：标题《%s》" % (name, (page.title() or "")[:40]))
    except Exception:
        pass
    filereqs = [u for u in seen if "/file/" in u][:6]
    if filereqs:
        log("%s：页面发出的文件请求示例：%s" % (name, " | ".join(u[:100] for u in filereqs)))
    else:
        log("%s：页面 8 秒内没发过任何 /file/ 请求（分享页可能没进到文档）" % name)
    import re as _re
    for u in seen:
        m = _re.search(r"/file/(\d{6,})/", u)
        if m:
            if m.group(1) != fid:
                log("%s：fileId 有更新（%s → %s），用新的" % (name, fid, m.group(1)))
                fid = m.group(1)
            break
    raw = b""
    for attempt in range(6):          # 登录态注入后一般一次就过；多试几轮防页面慢
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
                    log("%s：拿到直链并下载 %.2f MB（第 %d 次尝试，fid=%s）"
                        % (name, len(raw) / 1048576.0, attempt + 1, fid))
                    return raw
                log("%s：直链返回的不是文件（前 4 字节 %r）" % (name, raw[:4]))
        else:
            log("%s：第 %d 次还没拿到（status=%s，%s）"
                % (name, attempt + 1, st, txt[:80].replace("\n", " ")))
            if "permissionDenied" in txt and attempt == 2:
                break                 # 换 fid 也没戏，别空耗
    raise RuntimeError("%s：多次尝试都拿不到直链（fid=%s，登录态/分享权限检查一遍）" % (name, fid))


def load_kdocs_cookies():
    """从 worker /kdauth 取管理号机器上传的金山登录态（CDP 抓的 WebView2 cookies）。
    云端浏览器没有金山会话，匿名拉表实测被拒（userNotLogin），必须带这个。"""
    j = worker_req("/kdauth", timeout=60)
    cookies = j.get("cookies") or []
    pw_cookies = []
    for c in cookies:
        if not any(d in (c.get("domain") or "") for d in ("kdocs.cn", "wps.cn")):
            continue
        item = {"name": c["name"], "value": c["value"],
                "domain": c.get("domain"), "path": c.get("path") or "/"}
        exp = c.get("expires")
        if isinstance(exp, (int, float)) and exp > 0:
            item["expires"] = exp
        if c.get("httpOnly") is not None:
            item["httpOnly"] = bool(c["httpOnly"])
        if c.get("secure") is not None:
            item["secure"] = bool(c["secure"])
        ss = c.get("sameSite")
        if ss in ("Lax", "Strict", "None"):
            item["sameSite"] = ss
        pw_cookies.append(item)
    if not pw_cookies:
        raise RuntimeError("云端没有可用的金山登录态（/kdauth 为空或域名不符）")
    log("已取到金山登录态：%d 条 cookies（上传于 %s）"
        % (len(pw_cookies), time.strftime("%m-%d %H:%M", time.gmtime(j.get("up", 0) / 1000))))
    return pw_cookies


def build_mpayload(files_b64, log):
    """手机端 payload（2026-10-12）：用仓库里同源解析代码构建 {meta,rows,imgs}
    → POST /mpayload 上云（worker 对内容去重，没变不烧 KV 写额度）。
    LOCALAPPDATA 指到临时目录当工作目录（paths.wp() 的落点，CI 是 Linux）；
    图库 images_cache.json / 欣盛库 xinsheng.sqlite3 直接读仓库 app/assets。
    失败只告警不拖死 xlsx 链路。"""
    try:
        import shutil
        import sys
        import tempfile
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        work = tempfile.mkdtemp(prefix="mlwork_")
        os.environ["LOCALAPPDATA"] = work
        sys.path.insert(0, os.path.join(root, "app"))
        sys.path.insert(0, root)
        import paths
        wd = paths.work_dir()          # LOCALAPPDATA 下还有一层 ModelLookup，以此为准
        for name in ("data.xlsx", "price.xlsx", "subsidy.xlsx"):
            if name in files_b64:
                with open(os.path.join(wd, name), "wb") as f:
                    f.write(base64.b64decode(files_b64[name]))
        xs_src = os.path.join(root, "app", "assets", "xinsheng.sqlite3")
        if os.path.isfile(xs_src):
            shutil.copy(xs_src, os.path.join(wd, "xinsheng.sqlite3"))
        else:
            log("⚠ 仓库里没有欣盛库 app/assets/xinsheng.sqlite3，手机端这轮不带官方售价/补图")
        import payload as P
        from config import CLEAN_XLSX, PRICE_XLSX
        recs, month = [], ""
        for name in (CLEAN_XLSX, PRICE_XLSX):
            p = os.path.join(wd, name)
            if not os.path.isfile(p):
                continue
            r, mo = P.load_records_from_path(p)
            recs += r
            month = month or mo
            log("手机端：%s → %d 条记录" % (name, len(r)))
        if not recs:
            log("⚠ 手机端 payload：没有可用数据文件，跳过")
            return
        pl = P.build_payload(recs, P.load_img_library(), month)
        P.attach_subsidy(pl, log)
        P.attach_xinsheng(pl, log)
        body = {"meta": pl.get("meta") or {}, "rows": pl.get("rows") or [],
                "imgs": pl.get("imgs") or {}}
        j = worker_req("/mpayload", "POST", body, timeout=600)
        log("手机端 payload 上云：%s（rows=%d）%s"
            % (j.get("mb"), len(body["rows"]), "✔" if j.get("ok") else "✘ " + str(j.get("msg"))))
        if not j.get("ok"):
            raise RuntimeError(j.get("msg"))
    except Exception as e:
        log("⚠ 手机端 payload 构建失败（不影响 xlsx 链路）：%r" % e)


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
    failures = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        " (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
            locale="zh-CN", timezone_id="Asia/Shanghai")
        ctx.add_cookies(load_kdocs_cookies())   # 注入管理号的金山登录态
        page = ctx.new_page()
        for tab in TABLES:
            try:
                raw = fetch_xlsx(page, ctx, tab)
            except Exception as e:
                # 单表容错：一张被拒（比如分享设置了禁止下载）不要拖死另一张
                failures.append("%s：%s" % (tab["name"], e))
                log("⚠ %s 拉取失败，先上传拉到的部分：%s" % (tab["name"], e))
                continue
            files[tab["name"]] = base64.b64encode(raw).decode("ascii")
            hashes.append(hashlib.sha256(raw).hexdigest()[:12])
        browser.close()

    if not files:
        raise RuntimeError("两张表一张都没拉到：%s" % "; ".join(failures))
    if failures:
        OUT.write("⚠ 部分表格没拉到（软件端会继续用旧数据）：\n")
        for f in failures:
            OUT.write("- %s\n" % f)

    ts = "-".join(hashes)
    log("本次内容哈希 ts=%s（云端 %s）" % (ts, old_ts or "无"))
    if ts != old_ts:
        j = worker_req("/xlsxcloud", "POST",
                       {"ts": ts, "files": files}, timeout=300)
        if not j.get("ok"):
            raise RuntimeError("上传被拒：%s" % j.get("msg"))
        log("已上传云端（%.1f MB）——所有机器的软件 60 秒内自动跟随重建 ✔"
            % (j.get("mb") or 0))
    else:
        log("两张表内容都没变，跳过 xlsx 上传（机器不会有任何感知）✔")

    # 手机端 payload（2026-10-12）：每轮都构建上传，worker 内容去重省写额度；
    # 表没变也要走一遍——欣盛库/代码更新后手机端才能跟上
    build_mpayload(files, log)


if __name__ == "__main__":
    time.sleep(0)
    main()
