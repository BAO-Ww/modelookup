# -*- coding: utf-8 -*-
"""用友凭据与自动登录（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
独立变化源：换密码 / 换验证流程只碰这个文件。
依赖方向：yyclient 内部（login 依赖 transport / cloud / state / winref）。"""
import io
import json
import time

import state
import winref
from cloud import cloud_req, yy_alert_push
from config import YY_CAS_ENTRY, YY_PASS, YY_USER
from paths import wp
from yy_transport import _jsq, yy_capture_cookies, yy_on_c3, yy_on_login_page


def yy_creds():
    """自动登录用的账密，三级来源（2026-10-08 用户口径：账密挂云端跟管理号走，
    任何一台电脑登着管理号都能担起用友更新）：
    ① 内置常量 ② 本地 wp()/yy_auth.json ③ 云端 worker /yyauth（仅管理号可读，取回后缓存本地）。
    都没有 → None。"""
    if YY_USER and YY_PASS:
        return YY_USER, YY_PASS
    try:
        with io.open(wp("yy_auth.json"), encoding="utf-8") as f:
            d = json.load(f)
        if d.get("u") and d.get("p"):
            return str(d["u"]), str(d["p"])
    except Exception:
        pass
    j, err = cloud_req("/yyauth", timeout=30)
    if j and j.get("ok") and j.get("u") and j.get("p"):
        try:
            with io.open(wp("yy_auth.json"), "w", encoding="utf-8") as f:
                json.dump({"u": j["u"], "p": j["p"]}, f, ensure_ascii=False)
        except Exception:
            pass
        return str(j["u"]), str(j["p"])
    return None


def auto_yy_login(log):
    """用保存的账密在 YY 窗口里自动填 CAS 表单登录（2026-10-08）。
    成功建立会话返回 True；没凭据/没窗口/表单不对/有验证码 → False（调用方按原流程走）。"""
    c = yy_creds()
    if not c:
        return False
    u, p = c
    if winref.YY_WIN is None or winref.YY_CLOSED.is_set():
        return False
    try:
        log("用友会话失效，尝试用保存的账号自动登录…")
        state.set_state("running", "正在自动登录用友…")
        try:
            winref.YY_WIN.load_url(YY_CAS_ENTRY)
        except Exception:
            pass
        # 等页面定型：要么已经在 c3（会话其实还活着），要么落在 CAS 登录页
        on_login = False
        for _ in range(30):
            time.sleep(0.5)
            if winref.YY_CLOSED.is_set():
                return False
            if yy_on_c3(winref.YY_WIN):
                return True
            if yy_on_login_page(winref.YY_WIN):
                on_login = True
                break
        if not on_login:
            return False
        # 填表提交：用原生 setter 兼容 Vue/React 受控输入
        js = ("(function(){"
              "var u=document.querySelector('input[name=\\'username\\'],input[name=\\'loginName\\'],input[type=\\'text\\'],input[type=\\'email\\']');"
              "var p=document.querySelector('input[name=\\'password\\'],input[type=\\'password\\']');"
              "if(!u||!p) return 'NOFORM';"
              "var setv=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;"
              "setv.call(u,%s); u.dispatchEvent(new Event('input',{bubbles:true}));"
              "setv.call(p,%s); p.dispatchEvent(new Event('input',{bubbles:true}));"
              "var b=document.querySelector('button[type=\\'submit\\'],input[type=\\'submit\\'],#login-btn,.login-btn,button.btn-primary');"
              "if(b){b.click(); return 'CLICKED';}"
              "var f=u.closest('form'); if(f){f.submit(); return 'SUBMIT';}"
              "return 'NOBTN';})()") % (_jsq(u), _jsq(p))
        r = ""
        try:
            r = winref.YY_WIN.evaluate_js(js) or ""
        except Exception:
            pass
        log("自动登录提交结果：%s" % (r or "NOFORM"))
        if r not in ("CLICKED", "SUBMIT"):
            yy_alert_push("自动填表失败（%s），需要人工登录" % (r or "页面没有登录表单"), log)
            return False
        # 最多等 60 秒跳到 c3；还在登录页多半是密码错或冒了验证码 → 放弃走人工
        for _ in range(120):
            time.sleep(0.5)
            if winref.YY_CLOSED.is_set():
                return False
            if yy_on_c3(winref.YY_WIN):
                yy_capture_cookies(log)
                log("用友自动登录成功，会话已存住")
                return True
        yy_alert_push("自动登录没跳转成功（密码不对或要验证码），需要人工登录", log)
        return False
    except Exception as e:
        log("用友自动登录异常：%s" % e)
        return False
