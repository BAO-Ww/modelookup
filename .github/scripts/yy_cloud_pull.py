# -*- coding: utf-8 -*-
"""云端用友库存拉取器（GitHub Actions 定时任务跑）：
Playwright 起 Chromium → 用 CAS 账密自动登录用友 YonSuite → 分页拉全量库存 →
聚合成 payload.stocklive 同款结构 → 经 worker /stockcloud 上传（全员共享）。

流程与 app/main.py 的 do_stock_update 一致；聚合逻辑与 yy_finish 一致（norm_model 除外——
这里用简化归一：大写、去空格/全角空格、去颜色/套机后缀括号），请与前端 norm() 保持语义一致。
凭据从环境变量取（YY_USER / YY_PASS），由 GitHub Secrets 提供。
"""

import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

OUT = io.open(os.environ.get("GITHUB_STEP_SUMMARY") or os.devnull, "a", encoding="utf-8")

def log(*a):
    s = " ".join(str(x) for x in a)
    print(s, flush=True)
    OUT.write(str(s) + "\n")
    OUT.flush()

USER = os.environ["YY_USER"]
PASS = os.environ["YY_PASS"]
CLOUD = "https://auth.baozang.kdns.fr"
TOKEN = os.environ["CLOUD_TOKEN"]           # 管理号登录态（worker 鉴权用）

YY_CAS = ("https://euc.yonyoucloud.com/cas/login?sysid=diwork&multiCas=true"
          "&service=https%3A%2F%2Fc2.yonyoucloud.com%2Flogin")
YY_LIST = ("https://c3.yonyoucloud.com/yonbip-scm-stock/report/list"
           "?locale=zh_CN&serviceCode=stock_stockanalysislist&terminalType=1")
GROUP_SCHEMA = "2118595716194503889"
TPLID = "2159497746319933444"
K_MODEL = "oid_userDefine_2159497746314166282"
K_SKUNAME = "productsku_skuName"           # 物料SKU名称（含完整型号，带品类前缀）
K_QTY = "currentqty"
K_AVAIL = "availableqty"
K_WHTYPE = "oid_userDefine_2169743811705569285"
K_WH = "warehouse_name"
PAGE_SIZE = 1000
PAGE_MAX = 15


def norm_model(s):
    """型号归一：与 app/main.py norm_model 语义一致（大写、去空白、去全角）。
    ⚠ 2026-10-10 用户定稿「一字不差」：**不**截括号尾巴——(12T)星河U1 这类尾是型号本体，
      截了就跟别的配置混键（旧版在这里截括号是截短键的直接根因之一）。"""
    if not s:
        return ""
    s = str(s).strip().upper()
    s = s.replace("\u3000", " ").replace("　", " ")
    s = re.sub(r"[\uFF01-\uFF5E]", lambda c: chr(ord(c.group(0)) - 0xFEE0), s)
    return re.sub(r"\s+", "", s)


# ---------------------------------------------------------------- 浏览器登录

def login_and_fetch():
    from playwright.sync_api import sync_playwright

    log("== 启动 Chromium ==")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        " (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            locale="zh-CN", timezone_id="Asia/Shanghai")
        page = ctx.new_page()
        log("打开 CAS 登录页…")
        page.goto(YY_CAS, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        # 账密表单：username + password（手机号/用户名都能走）
        page.fill("input[name='username'], input[name='loginName']", USER, timeout=15000)
        page.fill("input[name='password'], input[type='password']", PASS, timeout=15000)
        log("账密已填，提交…")
        # 提交按钮：submit/btn-primary/login
        clicked = False
        for sel in ("button[type='submit']", "#login-btn", ".login-btn",
                    "button.btn-primary", "input[type='submit']"):
            try:
                b = page.locator(sel).first
                if b.count() and b.is_visible():
                    b.click()
                    clicked = True
                    break
            except Exception:
                continue
        if not clicked:
            # 兜底：回车提交
            page.press("input[type='password']", "Enter")
        page.wait_for_timeout(6000)

        # 登录成功判定：离开 cas/login；随后走 c2 → c3 SSO 链
        for _ in range(20):
            url = page.url
            if "cas/login" not in url:
                break
            page.wait_for_timeout(2000)
        log("登录后 URL:", page.url[:120])
        if "cas/login" in page.url:
            html = page.content()
            err = ""
            m = re.search(r"error_info\s*=\s*'([^']*)'", html)
            if m:
                err = m.group(1)
            log("❌ 登录未通过。error_info:", err or "(无)")
            shot = "/tmp/yy_fail.png"
            try:
                page.screenshot(path=shot, full_page=True)
                log("截图:", shot)
            except Exception:
                pass
            browser.close()
            sys.exit(2)

        # 确保 SSO 走到 c3 域（软件里实测：入口 service=c2/login 后自动跳 c3）
        log("访问 c3 工作台完成 SSO…")
        page.goto("https://c3.yonyoucloud.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        log("c3 URL:", page.url[:120])

        # 从页面上下文里拿 XSRF-TOKEN cookie
        xsrf = ""
        for c in ctx.cookies():
            if c["name"] == "XSRF-TOKEN":
                xsrf = c["value"]
                break
        log("XSRF-TOKEN:", "有" if xsrf else "无")
        if not xsrf:
            log("❌ 没拿到 XSRF-TOKEN（会话没建起来）")
            browser.close()
            sys.exit(3)

        # 分页拉全量（在 c3 域页面上下文里同步 XHR，同源自动带 cookie）
        all_rows = []
        total = None
        for pno in range(1, PAGE_MAX + 1):
            body = {
                "page": {"pageSize": PAGE_SIZE, "pageIndex": pno},
                "billnum": "stock_stockanalysis",
                "condition": {"commonVOs": []},
                "locale": "zh_CN",
                "serviceCode": "stock_stockanalysislist",
                "groupSchemaId": GROUP_SCHEMA,
                "isIncludeMeta": True,
                "ownDomain": "ustock",
                "tplid": TPLID,
                "bEmptyWithoutFilterTree": False,
            }
            expr = ("(async () => {"
                    "const r = await fetch('%s', {method:'POST', headers:{"
                    "'Content-Type':'application/json;charset=utf-8','Domain-Key':'ustock',"
                    "'X-XSRF-TOKEN':decodeURIComponent((document.cookie.match(/XSRF-TOKEN=([^;]+)/)||[])[1]||'')"
                    "}, body: JSON.stringify(%s)});"
                    "return JSON.stringify({st: r.status, txt: await r.text()});"
                    "})()") % (YY_LIST, json.dumps(body, ensure_ascii=False))
            res = json.loads(page.evaluate(expr))
            st = res["st"]
            txt = res["txt"]
            if st != 200 or not txt.strip().startswith("{"):
                log("第 %d 页异常（HTTP %s，疑似会话丢失）" % (pno, st))
                browser.close()
                sys.exit(4)
            d = json.loads(txt)
            if str(d.get("code")) != "200":
                log("API code:", d.get("code"), str(d.get("message"))[:120])
                browser.close()
                sys.exit(5)
            data = d.get("data") or {}
            rows = data.get("recordList") or []
            all_rows.extend(rows)
            total = data.get("recordCount")
            log("已拉 %d / %s 行（第 %d 页）" % (len(all_rows), total, pno))
            if (total and len(all_rows) >= total) or not rows:
                break
            page.wait_for_timeout(500)

        # 原始行落日志（2026-10-10 排查「L2 显示 3 台实际 2 台」）：关注型号逐行打全字段
        WATCHLIST = {"L2"}
        for r0 in all_rows:
            _mo = norm_model(r0.get(K_MODEL))
            base = _mo
            for w in WATCHLIST:
                if _mo == w or (_mo.startswith(w) and len(_mo) <= len(w) + 6):
                    log("WATCH %s 原始行：%s" % (
                        base, json.dumps(r0, ensure_ascii=False)))
                    break

        # 从浏览器侧拿全套 cookie（调试用）
        browser.close()
        return all_rows


# ---------------------------------------------------------------- 聚合 + 上传

def aggregate(recs):
    """聚合键（2026-10-10 与 app yy_finish 对齐的「一字不差」口径）：
    型号字段常被录成截短值（JSQ25-13TE7），完整名（JSQ25-13TE7(12T)星河U1）在
    物料SKU名称（productsku_skuName）里且带「海尔燃气热水器13升」前缀——
    从名称里定位型号子串、把它后面的尾巴拼回型号：key = mo + 名称中 mo 之后的尾部。"""
    agg = {}
    for r0 in recs:
        mo = norm_model(r0.get(K_MODEL))
        if not mo:
            continue
        sku = norm_model(r0.get(K_SKUNAME))
        if sku and mo in sku:
            tail = sku[sku.index(mo) + len(mo):]
            if tail:
                mo = mo + tail          # 完整名做键：型号字段截短时靠它一字不差对上
        a = agg.setdefault(mo, {"q": 0.0, "a": 0.0, "wt": {}, "whs": {}})
        try:
            a["q"] += float(r0.get(K_QTY) or 0)
        except (TypeError, ValueError):
            pass
        try:
            av = float(r0.get(K_AVAIL) or 0)
        except (TypeError, ValueError):
            av = 0.0
        a["a"] += av
        wt = str(r0.get(K_WHTYPE) or "").strip() or "其他仓"
        a["wt"][wt] = round(a["wt"].get(wt, 0.0) + av, 2)
        wname = str(r0.get(K_WH) or "").strip() or "未知仓"
        k2 = (wname, wt)
        a["whs"][k2] = round(a["whs"].get(k2, 0.0) + av, 2)
    for a in agg.values():
        lst = [{"w": k[0], "t": k[1], "n": v} for k, v in a["whs"].items()]
        lst.sort(key=lambda x: (-x["n"], x["w"]))
        a["whs"] = lst
    return agg


def cloud_req(path, method="GET", obj=None, timeout=180):
    body = json.dumps(obj).encode("utf-8") if obj is not None else None
    req = urllib.request.Request(CLOUD + path, data=body, method=method)
    req.add_header("X-Token", TOKEN)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0 Chrome/126.0")
    req.add_header("Origin", CLOUD)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8")), None
        except Exception:
            return None, "HTTP %d" % e.code
    except Exception as e:
        return None, str(e)


def main():
    ts = time.strftime("%m-%d %H:%M", time.localtime(time.time() + 8 * 3600))
    log("== 云端用友库存拉取 %s ==" % ts)
    recs = login_and_fetch()
    log("拉取完成：%d 行" % len(recs))
    # 排查日志（2026-10-10）：把含「13TE7」的原始行打出来，核对接口字段真值
    try:
        for r in recs:
            if "13TE7" in str(r.get(K_MODEL, "")).upper() or "13TE7" in str(r.get(K_SKUNAME, "")).upper():
                log("样本行：" + json.dumps(r, ensure_ascii=False)[:1500])
    except Exception as _e:
        log("样本日志失败：" + str(_e))
    agg = aggregate(recs)
    log("聚合：%d 个型号" % len(agg))
    j, err = cloud_req("/stockcloud", "POST",
                       {"ts": ts, "rows": len(recs), "stock": agg}, timeout=300)
    if err or not j or not j.get("ok"):
        log("❌ 上传云端失败：", err or (j or {}).get("msg"))
        sys.exit(6)
    log("✅ 已上传云端（%.1f MB，%d 个型号）——子账号自动跟进" % (
        j.get("mb") or 0, j.get("n") or len(agg)))


if __name__ == "__main__":
    main()
