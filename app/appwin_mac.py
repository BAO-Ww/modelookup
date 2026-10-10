# -*- coding: utf-8 -*-
"""Mac（macOS / Apple Silicon）窗口域 — pywebview + WKWebView（cocoa 后端）。
2026-10-11 Mac 端新增。与 appwin.py（Windows/winforms）平行，Windows 代码零改动；
跨平台部分（原生菜单 / 云端跟随线程 / 用友告警轮询 / xlsx 跟随）直接复用 appwin。

与 Windows 版的行为差异（有意为之，Mac 惯例）：
  · 关窗 = 真退出（无托盘收起；Dock 常驻由系统负责）
  · 开机自启走 LaunchAgent（~/Library/LaunchAgents），不是注册表
  · WKWebView 不支持 REMOTE_DEBUGGING_PORT，调试口子省略
  · Ctrl+W 收起的 js_api 桥保留（hide 到 Dock，点 Dock 图标/再启动可唤回）
依赖方向：appwin_mac 在 updater/cloud 之上（被 main 按平台分发调用）。
"""
import os
import plistlib
import subprocess
import sys
import threading
import time

import webview  # ⚠ 顶层 import（PyInstaller 规矩）

import state
import winref
from appwin import auto_updater, build_menu, yy_alert_loop    # 跨平台复用
from config import APP_NAME, HOST
from paths import is_frozen, wp
from state import _tick, set_state
from updater import do_update, xlsx_follower

_PLIST_LABEL = "cn.baozang.modelookup"


# ------------------------------------------------------------------ 屏幕/窗口

def work_area():
    """主显示器可用区域 (x, y, w, h)（AppKit；拿不到就给个保守默认）。"""
    try:
        from AppKit import NSScreen
        f = NSScreen.mainScreen().visibleFrame()
        x, y = int(f.origin.x), int(f.origin.y)
        w, h = int(f.size.width), int(f.size.height)
        if w > 200 and h > 200:
            return x, y, w, h
    except Exception:
        pass
    return 0, 0, 1440, 900


def fit_window(want_w, want_h):
    """窗口别比屏幕还大（同 Windows 版算法）。"""
    x, y, W, H = work_area()
    w = min(want_w, max(900, int(W * 0.94)))
    h = min(want_h, max(620, int(H * 0.94)))
    return w, h, x + (W - w) // 2, y + (H - h) // 2


def _safe_menu():
    """原生菜单（cocoa 出现在系统菜单栏）。万一 pywebview 版本不支持也不挡启动。"""
    try:
        return build_menu()
    except Exception as e:
        state.LOG.append("Mac 原生菜单创建失败（页面按钮仍可用）：%s" % e)
        return None


# ------------------------------------------------------------------ 开机自启（LaunchAgent）

def _bundle_path():
    """当前运行的 .app 包路径；开发模式返回 None。"""
    if not is_frozen():
        return None
    exe = os.path.abspath(sys.executable)
    marker = os.path.sep + "Contents" + os.path.sep + "MacOS" + os.path.sep
    if marker in exe:
        return exe.split(marker)[0]
    return None


def _plist_path():
    d = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")
    return os.path.join(d, _PLIST_LABEL + ".plist")


def _launchagent_conf(bundle):
    return {"Label": _PLIST_LABEL,
            "ProgramArguments": ["/usr/bin/open", "-a", bundle],
            "RunAtLoad": True}


def autostart_enabled():
    """LaunchAgent 存在且指向当前包 = 已开启。"""
    try:
        with open(_plist_path(), "rb") as f:
            old = plistlib.load(f)
        bundle = _bundle_path()
        return old.get("Label") == _PLIST_LABEL and (not bundle or
            old.get("ProgramArguments") == ["/usr/bin/open", "-a", bundle])
    except Exception:
        return False


def autostart_enable(log=None):
    """开机自启：写 LaunchAgent plist（open -a 拉起，Dock/菜单栏表现与手动打开一致）。"""
    try:
        if not is_frozen():
            return False
        bundle = _bundle_path()
        if not bundle or not os.path.isdir(bundle):
            if log:
                log("开机自启跳过：没找到 .app 包路径")
            return False
        os.makedirs(os.path.dirname(_plist_path()), exist_ok=True)
        conf = _launchagent_conf(bundle)
        if autostart_enabled():
            return True                      # 已配置好
        with open(_plist_path(), "wb") as f:
            plistlib.dump(conf, f)
        try:                                 # 让 launchd 立刻认识（失败下次登录生效）
            subprocess.Popen(["/bin/launchctl", "load", _plist_path()],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if log:
            log("已设置开机自启（LaunchAgent）：%s" % _plist_path())
        return True
    except Exception as e:
        if log:
            log("开机自启设置失败（不影响使用）：%s" % e)
        return False


def autostart_remove(log=None):
    try:
        try:
            subprocess.Popen(["/bin/launchctl", "unload", _plist_path()],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        if os.path.isfile(_plist_path()):
            os.remove(_plist_path())
        if log:
            log("已取消开机自启")
        return True
    except Exception as e:
        if log:
            log("取消开机自启失败：%s" % e)
        return False


# ------------------------------------------------------------------ 主流程

def run_app(httpd, port, startup_xlsx=None):
    url = "http://%s:%d/" % (HOST, port)

    class _JsBridge:
        """页面 → Python 桥（同 Windows 版接口）。Mac 上 Ctrl+W = 藏进 Dock，
        点 Dock 图标或再次启动（单实例 activate_existing）唤回。"""
        def hide_main(self):
            try:
                winref.MAIN_WIN.hide()
            except Exception:
                pass

    w, h, x, y = fit_window(1480, 940)
    lw, lh, lx, ly = fit_window(1180, 860)

    winref.MAIN_WIN = webview.create_window(
        APP_NAME,
        url=url,
        width=w, height=h, x=x, y=y,
        min_size=(900, 600),
        background_color="#f5f7fb",
        text_select=True,
        menu=_safe_menu(),
        js_api=_JsBridge(),
    )
    _tick("主窗口对象建好（cocoa/WKWebView）")
    # 登录窗口：先建好藏着（同 Windows 版，创建必须发生在 start() 之前）
    winref.LOGIN_WIN = webview.create_window(
        "登录金山文档 · 扫码后不用点任何按钮",
        html="<body style='background:#fff;font:14px sans-serif;padding:30px;color:#5b6577'>"
             "正在打开金山文档…</body>",
        width=lw, height=lh, x=lx, y=ly, min_size=(700, 520), hidden=True,
    )
    # 定时跟随线程（与 Windows 版完全同一套）
    threading.Thread(target=auto_updater, daemon=True).start()
    threading.Thread(target=xlsx_follower, daemon=True).start()
    threading.Thread(target=yy_alert_loop, daemon=True).start()

    def on_login_closed():
        winref.LOGIN_CLOSED.set()

    try:
        winref.LOGIN_WIN.events.closed += on_login_closed
    except Exception:
        pass

    winref.YY_WIN = webview.create_window(
        "用友登录 · 登录一次，之后点更新直接拉实时库存",
        html="<body style='background:#fff;font:14px sans-serif;padding:30px;color:#5b6577'>"
             "正在打开用友 YonSuite…</body>",
        width=520, height=680, hidden=True,
    )
    try:
        winref.YY_WIN.events.closed += lambda: winref.YY_CLOSED.set()
    except Exception:
        pass

    def on_main_closed():
        # Mac 惯例：关窗 = 退出。子窗口一并销毁、本地服务收摊。
        set_state("error", "窗口已关闭")
        for wref, ev in ((winref.LOGIN_WIN, winref.LOGIN_CLOSED),
                         (winref.YY_WIN, winref.YY_CLOSED)):
            try:
                if wref is not None and not ev.is_set():
                    wref.destroy()
            except Exception:
                pass
        try:
            httpd.shutdown()
        except Exception:
            pass
    try:
        winref.MAIN_WIN.events.closed += on_main_closed
    except Exception:
        pass

    def after_start():
        _tick("webview.start() 的 GUI 循环已起来（cocoa）")
        state.LOG.append(lambda m: print("[%s] %s" % (APP_NAME, m)))
        state.LOG.append(lambda m: None)
        # 开机自启默认开启（LaunchAgent）；没有托盘菜单，要关就用系统设置→登录项
        threading.Thread(target=autostart_enable, args=(state.LOG.append,),
                         daemon=True).start()
        if startup_xlsx:
            time.sleep(1.0)
            do_update(state.LOG.append, xlsx_path=startup_xlsx)

    _tick("准备调 webview.start()")
    # cocoa 不支持 REMOTE_DEBUGGING_PORT；private_mode=False 保住 localStorage
    # （「已发布」「我的定价」全在里面，端口不变的原因同 Windows 版）
    webview.start(after_start, gui="cocoa", debug=False,
                  private_mode=False, storage_path=wp("webview"),
                  http_server=False)
