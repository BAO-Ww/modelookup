# -*- coding: utf-8 -*-
"""用友取数传输层（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
JS 注入拉页 / 窗口位置判定 / cookie 三件套 / urllib 直拉 / 分页全量。
依赖方向：cloud → kdocs/yyclient（yyclient 内部：transport ← agg/login）。"""
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import state
from net import urlopen_ssl
import winref
from config import (YY_API_HOST, YY_COOKIE_FILE, YY_GROUP_SCHEMA, YY_LIST_PATH,
                    YY_PAGE_MAX, YY_PAGE_SIZE, YY_TPLID)
from paths import wp
from winref import js_eval


def _jsq(s):
    """把 Python 字符串安全塞进 JS 单引号字符串字面量。"""
    return str(s).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "")


def yy_fetch_page_js(page_index, page_size=YY_PAGE_SIZE):
    """在 c3 域的窗口里跑：同步 XHR 拉 report/list 一页，返回 responseText。"""
    body = json.dumps({
        "page": {"pageSize": page_size, "pageIndex": page_index},
        "billnum": "stock_stockanalysis",
        "condition": {"commonVOs": []},
        "locale": "zh_CN",
        "serviceCode": "stock_stockanalysislist",
        "groupSchemaId": YY_GROUP_SCHEMA,
        "isIncludeMeta": True,
        "ownDomain": "ustock",
        "tplid": YY_TPLID,
        "bEmptyWithoutFilterTree": False,
    }, ensure_ascii=False)
    return ("(function(){try{"
            "var m=document.cookie.match(/XSRF-TOKEN=([^;]+)/);"
            "var t=m?decodeURIComponent(m[1]):'';"
            "var x=new XMLHttpRequest();"
            "x.open('POST','" + YY_LIST_PATH + "',false);"
            "x.setRequestHeader('Content-Type','application/json;charset=utf-8');"
            "x.setRequestHeader('Domain-Key','ustock');"
            "x.setRequestHeader('X-XSRF-TOKEN',t);"
            "x.send('" + _jsq(body) + "');"
            "return x.status+'|'+String(x.responseText);"
            "}catch(e){return 'ERR|'+e.message;}})()")


def yy_call(win, page_index, page_size=YY_PAGE_SIZE):
    """返回 (data_dict, None) 或 (None, 错误串)。err='NOTLOGIN' 表示没登录。"""
    r = js_eval(win, yy_fetch_page_js(page_index, page_size))
    if r is None:
        return None, "执行脚本失败"
    r = str(r)
    if r.startswith("ERR|"):
        return None, r[4:]
    st, _, txt = r.partition("|")
    txt = txt.strip()
    if not txt.startswith("{"):
        return None, "NOTLOGIN"          # 被重定向到 CAS 登录页了（返回 HTML）
    try:
        d = json.loads(txt)
    except Exception:
        return None, "返回非 JSON：" + txt[:120]
    if str(d.get("code")) != "200":
        msg = str(d.get("message") or ("HTTP " + st))
        if "401" in msg or "登录" in msg or "token" in msg.lower():
            return None, "NOTLOGIN"
        return None, msg
    return d.get("data") or {}, None


def yy_on_login_page(win):
    u = str(js_eval(win, "location.href", "") or "")
    return ("cas/login" in u) or ("c2.yonyoucloud.com" in u and "/login" in u)


def yy_on_c3(win):
    u = str(js_eval(win, "location.href", "") or "")
    return u.startswith("https://c3.yonyoucloud.com")


def yy_cookies_load():
    p = wp(YY_COOKIE_FILE)
    if os.path.isfile(p):
        try:
            with io.open(p, encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d.get("cookies"), dict) and d["cookies"]:
                return d
        except Exception:
            pass
    return None


def yy_cookies_save(pairs):
    try:
        with io.open(wp(YY_COOKIE_FILE), "w", encoding="utf-8") as f:
            json.dump({"ts": time.strftime("%m-%d %H:%M"), "cookies": pairs},
                      f, ensure_ascii=False)
    except Exception:
        pass


def yy_capture_cookies(log):
    """登录成功后从用友窗口抓 cookie 存盘（SimpleCookie 列表 → {name: value}）。"""
    try:
        cks = winref.YY_WIN.get_cookies() or []
    except Exception as e:
        log("抓 cookie 失败：%s（重启后可能要重登一次）" % e)
        return
    pairs = {}
    for sc in cks:
        try:
            for name, morsel in sc.items():
                if name and morsel.value:
                    pairs[name] = morsel.value
        except Exception:
            continue
    if pairs:
        yy_cookies_save(pairs)
        log("已保存用友会话（%d 个 cookie）——以后开软件不用再登录" % len(pairs))


def yy_call_direct(page_index, page_size=YY_PAGE_SIZE, timeout=40):
    """不走 WebView：urllib 带保存的 Cookie 直拉 report/list。
    返回 (data, None) / (None, err)，err='NOTLOGIN' 表示会话失效。"""
    saved = yy_cookies_load()
    if not saved:
        return None, "NOTLOGIN"
    pairs = saved["cookies"]
    body = json.dumps({
        "page": {"pageSize": page_size, "pageIndex": page_index},
        "billnum": "stock_stockanalysis",
        "condition": {"commonVOs": []},
        "locale": "zh_CN",
        "serviceCode": "stock_stockanalysislist",
        "groupSchemaId": YY_GROUP_SCHEMA,
        "isIncludeMeta": True,
        "ownDomain": "ustock",
        "tplid": YY_TPLID,
        "bEmptyWithoutFilterTree": False,
    }, ensure_ascii=False).encode("utf-8")
    hdrs = {
        "Content-Type": "application/json;charset=utf-8",
        "Domain-Key": "ustock",
        "Cookie": "; ".join("%s=%s" % (k, v) for k, v in pairs.items()),
        "Referer": YY_API_HOST + "/",
        "Origin": YY_API_HOST,
    }
    xv = pairs.get("XSRF-TOKEN") or ""
    if xv:
        hdrs["X-XSRF-TOKEN"] = urllib.parse.unquote(xv)
    req = urllib.request.Request(YY_API_HOST + YY_LIST_PATH, data=body, headers=hdrs)
    try:
        with urlopen_ssl(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) or "login" in (e.url or ""):
            return None, "NOTLOGIN"
        return None, "HTTP %d" % e.code
    except Exception as e:
        return None, str(e) or repr(e)
    txt = txt.strip()
    if not txt.startswith("{"):
        return None, "NOTLOGIN"
    try:
        d = json.loads(txt)
    except Exception:
        return None, "返回非 JSON：" + txt[:120]
    if str(d.get("code")) != "200":
        msg = str(d.get("message") or "接口返回异常")
        if "401" in msg or "登录" in msg or "token" in msg.lower():
            return None, "NOTLOGIN"
        return None, msg
    return d.get("data") or {}, None


def yy_pull_all(call_fn, log):
    """分页拉全量（默认查询 = 全仓库类型）。call_fn(page_index) → (data, err)。"""
    recs = []
    total = None
    page = 1
    while page <= YY_PAGE_MAX:
        data, err = call_fn(page)
        if err:
            return None, err
        rows = data.get("recordList") or []
        recs.extend(rows)
        if page == 1:
            # 第 1 页原始行立即落盘（排查接口字段用，不依赖后续聚合是否成功）
            try:
                with io.open(wp("yy_page1.json"), "w", encoding="utf-8") as f:
                    json.dump({"ts": time.strftime("%m-%d %H:%M:%S"),
                               "total": data.get("recordCount"),
                               "nfields": list(rows[0].keys()) if rows else [],
                               "rows": rows[:8]}, f, ensure_ascii=False, indent=1)
            except Exception as _e:
                try:
                    log("第1页样本落盘失败：" + str(_e))
                except Exception:
                    pass
        total = data.get("recordCount")
        log("实时库存：已拉 %d / %s 行（第 %d 页）" % (len(recs), total, page))
        state.set_state("running", "实时库存：已拉 %d / %s 行…" % (len(recs), total if total else len(recs)))
        if total and len(recs) >= total:
            break
        if not rows:
            break
        page += 1
        time.sleep(0.4)
    return recs, None
