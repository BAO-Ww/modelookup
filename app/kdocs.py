# -*- coding: utf-8 -*-
"""金山文档（kdocs）取数客户端（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
依赖方向：… → cloud → kdocs（与 yyclient 同层；只依赖 config/state/winref）。"""
import io
import json
import os
import time

import state
import winref
from config import FILE_ID, ONLINE_SHARE
from paths import wp

# 在「已经是 kdocs 域」的窗口里跑同步 XHR —— 同源，自动带上该窗口的登录态，
# 不用把 cookie 搬到 Python 这边来（陌生电脑扫码后一样能用）
def sync_xhr_js(file_id):
    return ("(function(){try{"
            "var x=new XMLHttpRequest();"
            "x.open('GET','/api/v3/office/file/" + str(file_id) + "/download',false);"
            "x.send();"
            "return x.status+'|'+String(x.responseText);"
            "}catch(e){return 'ERR|'+e.message;}})()")


# 从分享链接反查 fileId：页面自己会去请求 /api/v3/office/file/<fileId>/…，
# 抓 performance 里的 URL 就能捞出来（比让用户手工填靠谱）
PROBE_FID_JS = (
    "(function(){try{var a=performance.getEntries()||[];var s='';"
    "for(var i=0;i<a.length;i++){s+=String(a[i].name)+'\\n';}"
    "var m=s.match(/\\/file\\/(\\d{6,})/);return m?m[1]:'';}catch(e){return '';}})()"
)


def kdocs_ready(win):
    return str(winref.js_eval(win, "location.host", "") or "").endswith("kdocs.cn")


def direct_url_from_window(log, file_id):
    """返回 (直链, 错误)。错误 'NOTLOGIN' 表示窗口没登录。"""
    if winref.LOGIN_WIN is None or winref.LOGIN_CLOSED.is_set():
        return None, "登录窗口不可用"
    if not kdocs_ready(winref.LOGIN_WIN):
        return None, "NOTLOGIN"
    r = winref.js_eval(winref.LOGIN_WIN, sync_xhr_js(file_id))
    if r is None:
        return None, "执行脚本失败"
    r = str(r)
    if r.startswith("ERR|"):
        return None, r[4:]
    _, _, txt = r.partition("|")
    if txt[:2] == "PK":
        return None, "端点直接返回了文件"
    try:
        d = json.loads(txt)
    except Exception:
        return None, "返回非 JSON：" + txt[:160]
    if str(d.get("result")).lower() in ("usernotlogin", "notlogin"):
        return None, "NOTLOGIN"
    du = d.get("download_url") or d.get("url") or ""
    if isinstance(du, str) and du.startswith("http"):
        return du, None
    return None, "拿不到直链：" + txt[:160]


def open_login_window(log, share_url):
    """把登录窗口导航到在线表所在域，等页面就绪。"""
    state.set_state("running", "正在打开金山文档…")
    log("打开金山文档页面…")
    winref.LOGIN_WIN.load_url(share_url)
    for _ in range(40):
        time.sleep(0.5)
        if winref.LOGIN_CLOSED.is_set():
            return False
        if kdocs_ready(winref.LOGIN_WIN) and winref.js_eval(winref.LOGIN_WIN, "document.readyState") == "complete":
            time.sleep(1.0)
            return True
    return kdocs_ready(winref.LOGIN_WIN)


def obtain_download_url(log, file_id=FILE_ID, share_url=ONLINE_SHARE,
                        allow_prompt=True, wait_s=900, silent=8):
    """拿到 xlsx 的签名直链。已登录会静默完成；未登录则弹窗等人扫码。"""
    if winref.LOGIN_WIN is None or winref.LOGIN_CLOSED.is_set():
        raise RuntimeError("登录窗口不可用，无法从在线表更新")

    if not kdocs_ready(winref.LOGIN_WIN):
        open_login_window(log, share_url)

    # 先静默重试：在线表是公开分享的，页面加载后十几秒匿名会话往往就能拿到直链
    # —— 别一上来就弹扫码窗吓人，绝大部分人根本不用登录
    state.set_state("running", "正在连接在线表…（如果需要登录，会自动弹出扫码窗口）")
    for i in range(max(silent, 1)):
        du, err = direct_url_from_window(log, file_id)
        if du:
            log("静默取到直链（用时约 %d 秒）" % (i * 2))
            return du
        if i == 0 and err != "NOTLOGIN":
            log("直链异常：%s（继续重试）" % err)
        time.sleep(2.5)

    if not allow_prompt:
        raise RuntimeError("拿不到下载直链（在线表可能需要登录）")

    log("静默期没拿到：弹出登录窗口等人扫码")
    try:
        winref.LOGIN_WIN.show()
    except Exception:
        pass
    state.set_state("login", "请在弹出的窗口里用微信/手机扫码登录金山文档，登录后会自动继续（无需点任何按钮）")

    t0 = time.time()
    tick = 0
    while time.time() - t0 < wait_s:
        time.sleep(3)
        if winref.LOGIN_CLOSED.is_set():
            raise RuntimeError("登录窗口被关闭，已取消更新")
        du, err = direct_url_from_window(log, file_id)
        if du:
            log("登录成功，取到直链")
            try:
                winref.LOGIN_WIN.hide()
            except Exception:
                pass
            return du
        tick += 1
        if tick % 4 == 0:
            left = int((wait_s - (time.time() - t0)) / 60)
            state.set_state("login", "等待扫码登录中…（还剩约 %d 分钟；登录成功后自动继续）" % max(left, 1))
    try:
        winref.LOGIN_WIN.hide()
    except Exception:
        pass
    raise RuntimeError("等待登录超时，已取消更新")


def probe_file_id(log, share_url, cache_name="price_fileid.txt"):
    """打开分享页 → 从它自己发出的请求里反查 fileId → 缓存到工作目录。"""
    p = wp(cache_name)
    if os.path.isfile(p):
        try:
            t = io.open(p, encoding="utf-8").read().strip()
            if t.isdigit():
                return t
        except Exception:
            pass
    if winref.LOGIN_WIN is None or winref.LOGIN_CLOSED.is_set():
        return None
    log("正在打开价格表分享页，反查 fileId…")
    winref.LOGIN_WIN.load_url(share_url)
    for _ in range(40):
        time.sleep(0.5)
        if winref.LOGIN_CLOSED.is_set():
            return None
        if kdocs_ready(winref.LOGIN_WIN) and winref.js_eval(winref.LOGIN_WIN, "document.readyState") == "complete":
            break
    time.sleep(1.5)
    fid = str(winref.js_eval(winref.LOGIN_WIN, PROBE_FID_JS) or "").strip()
    if not fid.isdigit():
        log("没探到价格表的 fileId（分享页可能改版了）")
        return None
    try:
        with io.open(p, "w", encoding="utf-8") as f:
            f.write(fid)
    except Exception:
        pass
    log("价格表 fileId = %s" % fid)
    return fid
