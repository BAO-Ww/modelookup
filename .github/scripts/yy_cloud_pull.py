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
CLOUD = "https://auth.tingjiabaobao.de5.net"
TOKEN = os.environ["CLOUD_TOKEN"]           # 管理号登录态（worker 鉴权用）

YY_CAS = ("https://euc.yonyoucloud.com/cas/login?sysid=diwork&multiCas=true"
          "&service=https%3A%2F%2Fc2.yonyoucloud.com%2Flogin")
YY_LIST = ("https://c3.yonyoucloud.com/yonbip-scm-stock/report/list"
           "?locale=zh_CN&serviceCode=stock_stockanalysislist&terminalType=1")
GROUP_SCHEMA = "2118595716194503889"
TPLID = "2159497746319933444"
K_MODEL = "oid_userDefine_2159497746314166282"
K_SKUNAME = "productsku_skuName"           # 物料SKU名称（含完整型号，带品类前缀）
K_CODE = "productsku_cCode"                # 物料编码（A08HR01163）——SKU 唯一码，
                                           # 软件表 YS物料编码 就是它：编码一对一硬核对的主键
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

def _clean_base(s):
    """型号字段剥中文尾（与 app core_of 同款）：BC/BD-100GHPG9Z小富豪 → BC/BD-100GHPG9Z。
    ⚠ 截完必须 rstrip 分隔符，否则 BCD-550WGCFDM4WHU1(棕) 会留下拖尾「(」。"""
    m = re.search(r"[\u4e00-\u9fff]", s)
    if m and m.start() >= 3:
        return s[: m.start()].rstrip("(-_/+.,、，")
    return s


def aggregate(recs, t_mo=None):
    """聚合键（2026-10-10 晚「型号表当裁判」口径，与 app yy_finish 对齐）：
    ERP「型号」字段大多干净（BC/BD-629GHEP），但有的行字段本身就带中文尾
    （BC/BD-100GHPG9Z小富豪），有的截短（JSQ25-13TE7），有的中文尾是型号本体
    （星光棕/套机/星河U1）。三级候选按序取第一个在型号表里的：
      ① 拼接键（型号字段 + skuName 里型号之后的尾巴）——截短补全靠它；
      ② 型号字段原样——skuName 带描述尾缀（…大富豪系列）时靠它；
      ③ 型号字段剥中文尾——型号字段自身带描述尾（…小富豪）时靠它。
    都不在表里 → 维持①（宁可键脏也别丢数量，前端还有尾缀兜底匹配）。

    同时按**物料编码**多聚一份 agg_code（2026-10-10 深夜用户定稿「编码优先」）：
    productsku_cCode 是 SKU 唯一码、软件表 YS物料编码 就是它——编码一对一硬核对，
    没有任何启发式解释空间。型号键原封不动保留（编码没覆盖的 41% 行走型号兜底）。
    返回 (agg, agg_code)。"""
    agg = {}
    agg_code = {}

    for r0 in recs:
        mo = norm_model(r0.get(K_MODEL))
        if not mo:
            continue
        sku = norm_model(r0.get(K_SKUNAME))
        full = mo
        if sku and mo in sku:
            tail = sku[sku.index(mo) + len(mo):]
            if tail:
                full = mo + tail          # 完整名候选：型号字段截短时靠它一字不差对上
        key = full
        if t_mo:
            if full in t_mo:
                key = full
            elif mo in t_mo:
                key = mo
            else:
                mo_c = _clean_base(mo)
                if mo_c and mo_c in t_mo:
                    key = mo_c
        a = agg.setdefault(key, {"q": 0.0, "a": 0.0, "wt": {}, "whs": {}})
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
        # 编码维度并行聚一份（同一行既进型号桶也进编码桶）
        code = norm_model(r0.get(K_CODE))
        if code and not code.startswith("#"):          # 排除 #N/A 脏值
            ac = agg_code.setdefault(code, {"q": 0.0, "a": 0.0, "wt": {}, "whs": {}})
            try:
                ac["q"] += float(r0.get(K_QTY) or 0)
            except (TypeError, ValueError):
                pass
            ac["a"] += av
            ac["wt"][wt] = round(ac["wt"].get(wt, 0.0) + av, 2)
            ac["whs"][k2] = round(ac["whs"].get(k2, 0.0) + av, 2)
    for a in agg.values():
        lst = [{"w": k[0], "t": k[1], "n": v} for k, v in a["whs"].items()]
        lst.sort(key=lambda x: (-x["n"], x["w"]))
        a["whs"] = lst
    for a in agg_code.values():
        lst = [{"w": k[0], "t": k[1], "n": v} for k, v in a["whs"].items()]
        lst.sort(key=lambda x: (-x["n"], x["w"]))
        a["whs"] = lst
    return agg, agg_code


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


def fetch_model_set():
    """从 worker /xlsxcloud 拉 data.xlsx（金山清理明细）+ price.xlsx（LYDF 价格表），
    解析「型号」列 → 归一化集合；顺带收「YS物料编码/商品编码」列（编码匹配率统计用）。
    这是聚合键的裁判表：ERP 干净型号在表里就用干净键（剥掉「…大富豪系列」这类描述尾缀）。
    ⚠ 两张表都要：小富豪×3/全空间×2 这几个型号只在 LYDF 价格表里、清理明细没有，
      只扫 data.xlsx 会让它们的 ERP 键残留脏尾缀。
    拉不到 / 解析不了 → 返回已有部分（或 None），聚合退回旧拼接口径（不影响出数）。
    返回 (型号集合或 None, 卡片编码集合)。"""
    card_codes = set()
    try:
        import base64
        import openpyxl
        j, err = cloud_req("/xlsxcloud", "GET", timeout=300)
        if err or not (j or {}).get("ok"):
            log("⚠ 型号表拉取失败（%s），聚合退回拼接口径" % (err or (j or {}).get("msg")))
            return None, card_codes
        files = j.get("files") or {}
        out = set()
        CODE_HEADERS = ("YS物料编码", "商品编码", "编码")

        def harvest(b64, sheet_filter=None, header_scan=4, need_all=("型号",)):
            """通用收型号：在 header_scan 行内找含 need_all 的表头行，收型号列；
            同 sheet 里见到编码列（YS物料编码/商品编码/编码）也顺手收进 card_codes。"""
            if not b64:
                return
            wb = openpyxl.load_workbook(io.BytesIO(base64.b64decode(b64)),
                                        read_only=True, data_only=True)
            for ws in wb.worksheets:
                # ⚠ 千万别全簿瞎扫：data.xlsx 里有 Sheet9 这种 104 万行的空壳 sheet，
                #   read_only 逐行迭代会跑几十分钟把 job 拖死（本机实测 SIGTERM）。
                if sheet_filter and not sheet_filter(ws.title):
                    continue
                rows = list(ws.iter_rows(values_only=True))
                hi = i_mo = None
                i_code = None
                for ri in range(min(header_scan, len(rows))):
                    names = [str(x).strip() if x is not None else "" for x in (rows[ri] or [])]
                    if all(n in names for n in need_all):
                        hi, i_mo = ri, names.index("型号")
                        for ch in CODE_HEADERS:
                            if ch in names:
                                i_code = names.index(ch)
                                break
                        break
                if hi is None:
                    continue
                for r in rows[hi + 1:]:
                    v = r[i_mo] if i_mo is not None and i_mo < len(r) else None
                    m = norm_model(v)
                    if m and m != "型号":
                        out.add(m)
                    if i_code is not None and i_code < len(r):
                        cc = norm_model(r[i_code])
                        if cc and not cc.startswith("#"):
                            card_codes.add(cc)
            wb.close()

        # 清理明细（滞销/样机）：软件型号表的主来源
        harvest(files.get("data.xlsx"), sheet_filter=lambda t: "清理" in (t or ""))
        # LYDF 价格表：型号字段截短值 + 部分型号只在这里有（小富豪/全空间…）。
        # 表头「型号+品牌」在 r2（美的在 r1）；用「型号+品牌」判据正好跳过现存量10.6 旧 sheet
        harvest(files.get("price.xlsx"), header_scan=15,
                need_all=("型号", "品牌"))
        log("型号表裁判集：%d 个型号 · 卡片编码 %d 个" % (len(out), len(card_codes)))
        return (out or None), card_codes
    except Exception as e:
        log("⚠ 型号表解析失败（%s），聚合退回拼接口径" % e)
        return None, card_codes


def main():
    # 北京时间 = UTC+8，显式算（2026-10-18 修复：原 localtime+8 在云端 UTC 机器上正确，
    # 挪到本机 self-hosted runner 后 localtime 已是北京时间，再加 8 就变成"未来时间"）
    ts = time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 8 * 3600))
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
    t_mo, card_codes = fetch_model_set()
    agg, agg_code = aggregate(recs, t_mo)
    log("聚合：%d 个型号 · %d 个编码" % (len(agg), len(agg_code)))
    # 编码直连率：卡片编码能在库存编码桶里直接找到的（前端编码优先路径的实际命中率）
    if card_codes:
        hitc = sum(1 for c in card_codes if c in agg_code)
        log("编码直连：%d / %d 个卡片编码命中（%.0f%%）" % (hitc, len(card_codes), 100.0 * hitc / len(card_codes)))
    j, err = cloud_req("/stockcloud", "POST",
                       {"ts": ts, "rows": len(recs), "stock": agg, "sbc": agg_code}, timeout=300)
    if err or not j or not j.get("ok"):
        log("❌ 上传云端失败：", err or (j or {}).get("msg"))
        sys.exit(6)
    log("✅ 已上传云端（%.1f MB，%d 个型号）——子账号自动跟进" % (
        j.get("mb") or 0, j.get("n") or len(agg)))


if __name__ == "__main__":
    main()
