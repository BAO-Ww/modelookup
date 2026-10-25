# -*- coding: utf-8 -*-
"""窗口 / 应用生命周期（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
窗口尺寸 / 原生菜单 / 托盘 / 自动跟随线程 / run_app。
依赖方向：appwin 在 updater/cloud 之上（被 server 的 POST 路由与入口 main() 调用）。
⚠ 不再细拆：setup_tray/_wire_tray/guarded 是 run_app 的内嵌套函数，拆出必须重构签名
  传 win/tray 引用——会触碰原本不用动的代码（拆分设计.md 附录明令不拆）。"""
import os
import sys
import threading
import time

import webview  # ⚠ 顶层 import（PyInstaller 规矩）

import state
import winref
from cloud import stock_pull_cloud, yy_alert_poll
from config import APP_NAME, APP_VERSION, HOST, ONLINE_SHARE, PRICE_SHARE, RUN_KEY
from paths import is_frozen, rp, wp
from payload import get_payload
from state import _tick, has_perm, read_state, set_state
from updater import do_update, xinsheng_follower, xlsx_follower


def _dpi():
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def work_area():
    """主显示器可用区域 (x, y, w, h)。"""
    try:
        import ctypes
        import ctypes.wintypes as wt
        r = wt.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0):
            w, h = r.right - r.left, r.bottom - r.top
            if w > 200 and h > 200:
                return r.left, r.top, w, h
    except Exception:
        pass
    return 0, 0, 1280, 800


def fit_window(want_w, want_h):
    """窗口别比屏幕还大（远程桌面 / 小屏笔记本上很常见）。"""
    x, y, W, H = work_area()
    w = min(want_w, max(900, int(W * 0.94)))
    h = min(want_h, max(620, int(H * 0.94)))
    return w, h, x + (W - w) // 2, y + (H - h) // 2


def _webview2_pv():
    """已安装的 WebView2 运行时版本（没有就返回 None）。"""
    try:
        import winreg
        keys = [
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
                                       r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
                                       r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
                                      r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        ]
        for root, sub in keys:
            try:
                with winreg.OpenKey(root, sub) as k:
                    v = winreg.QueryValueEx(k, "pv")[0]
                if v and v != "0.0.0.0":
                    return v
            except OSError:
                continue
    except Exception:
        pass
    return None


def _wire_menu_autoclose():
    """原生菜单鼠标移出自动收起（2026-10-09 用户要求：点开菜单后鼠标离开老是不收）。
    给每个顶级菜单项的下拉挂 MouseLeave → Close；菜单重画后必须重新挂。"""
    bv = getattr(winref.MAIN_WIN, "native", None)
    if bv is None:
        return

    def _do():
        try:
            import System.Windows.Forms as WF
            n = 0
            for c in list(bv.Controls):
                if not isinstance(c, WF.MenuStrip):
                    continue
                for item in c.Items:
                    if isinstance(item, WF.ToolStripMenuItem) and item.HasDropDownItems:
                        dd = item.DropDown

                        def _close(sender=None, e=None, _dd=dd):
                            try:
                                _dd.Close()
                            except Exception:
                                pass

                        dd.MouseLeave += _close
                        n += 1
            if n:
                state.LOG.append("菜单自动收起已挂到 %d 个下拉" % n)
        except Exception as e:
            state.LOG.append("菜单自动收起挂接失败：%s" % e)

    try:
        from System import Func, Type
        if bv.InvokeRequired:
            bv.Invoke(Func[Type](_do))
        else:
            _do()
    except Exception:
        _do()


def rebuild_menu_for(role):
    """登录后按角色重画原生菜单（2026-10 八次改版）：
    子账号只留 账号管理/退出登录，管理员恢复全量。winforms 的 set_window_menu
    只会往 Controls 里加 MenuStrip，重画前要把旧的摘掉。"""
    try:
        bv = getattr(winref.MAIN_WIN, "native", None)
        if bv is None:
            return
        menus = build_menu(sub=(role != "admin"))

        def _clear():
            try:
                import System.Windows.Forms as WF
                for c in list(bv.Controls):
                    if isinstance(c, WF.MenuStrip):
                        bv.Controls.Remove(c)
            except Exception:
                pass

        try:
            from System import Func, Type
            if bv.InvokeRequired:
                bv.Invoke(Func[Type](_clear))   # winforms 后端同款写法
            else:
                _clear()
        except Exception:
            try:
                _clear()
            except Exception:
                pass
        bv.set_window_menu(menus)
        _wire_menu_autoclose()
        state.LOG.append("菜单已按角色重画：%s" % ("管理号全量" if role == "admin" else "子账号精简"))
    except Exception as e:
        state.LOG.append("菜单重画失败：%s" % e)


def build_menu(sub=False):
    """sub=True 返回子账号的精简菜单（只有 账号管理/退出登录，用户要求）。"""
    from webview.menu import Menu, MenuAction, MenuSeparator

    # 2026-10-12：&助记符是 Windows 惯例，Mac 菜单栏不认，会把「数据(&D)」原样
    # 显示出字面量 &。darwin 下包一层把 & 剥掉；Windows 路径零变化。
    if sys.platform == "darwin":
        _Menu, _MenuAction = Menu, MenuAction

        def Menu(label, items):                      # noqa: F811
            return _Menu(str(label).replace("&", ""), items)

        def MenuAction(label, *a, **kw):             # noqa: F811
            return _MenuAction(str(label).replace("&", ""), *a, **kw)

    def guarded(name, need):
        """菜单点按先过权限：不满足就弹提示（菜单是原生建的，创建后没法动态藏，
        所以用「点了没权限就拦」的方式，效果一样——子账号点不动）。"""
        if not has_perm(need):
            webview.windows[0].evaluate_js(
                "alert('「%s」只有管理员能用，请联系管理员。')" % name)
            return False
        return True

    def upd_all():
        if not guarded("全部更新", "upd_kdocs"):
            return
        js_click("allbtn")

    def upd():
        if not guarded("从金山文档更新", "upd_kdocs"):
            return
        # 2026-10-09 v2.8 全云端口径：菜单 = 触发云端（GitHub Actions 拉金山），
        # 跑完本机自动同步重建；进度/结果都显示在侧栏底部（startCloud）
        try:
            webview.windows[0].evaluate_js("startCloud('kd')")
        except Exception:
            pass

    def upd_stock():
        # 2026-10-09 v2.8：同上 —— 触发云端（GitHub Actions 拉用友），跑完自动同步
        try:
            webview.windows[0].evaluate_js("startCloud('yy')")
        except Exception:
            pass

    def upd_app():
        """软件自更新：打开更新弹窗（检查/下载/安装全程可见）。
        旧实现去点 #appupd 隐藏按钮，反馈只有侧栏一行小字，用户以为用不了。"""
        try:
            webview.windows[0].evaluate_js("openAppUpdate()")
        except Exception:
            pass

    def reload_page():
        # 2026-10-09：子账号菜单也有这项（用户要求），刷新页面无副作用，不再过权限
        try:
            webview.windows[0].evaluate_js("location.reload()")
        except Exception:
            pass

    def about():
        webview.windows[0].evaluate_js(
            "alert('型号查询 v%s\\n\\n滞销/样机清理明细\\n"
            "数据来源：金山文档在线表（公布版 + LYDF 价格表）\\n内置 %s')"
            % (APP_VERSION, _built_desc())
        )

    def source():
        if not guarded("打开在线表（公布版）", "upd_kdocs"):
            return
        try:
            import webbrowser
            webbrowser.open(ONLINE_SHARE)
        except Exception:
            pass

    def source_price():
        if not guarded("打开在线表（价格表）", "upd_kdocs"):
            return
        try:
            import webbrowser
            webbrowser.open(PRICE_SHARE)
        except Exception:
            pass

    def js_click(elid):
        webview.windows[0].evaluate_js(
            "(function(){var b=document.getElementById('%s'); if(b) b.click();})()" % elid)

    def adm():
        # 管理员 → openAdmin 全量面板；子账号 → openSelf 改密小面板（模板里按角色分流）
        js_click("admbtn")

    def logout():
        js_click("whoami")

    def selfdisp():
        # 我的显示开关（2026-10-09 用户要求：入口放顶部菜单栏，顶栏不放）
        # 2026-10-10：直接 evaluate_js 调 openSelfDisplay() 用户实测点不开，
        # 改走与 adm() 相同的 js_click 链路（点页面里的隐藏按钮 selfdispbtn）
        js_click("selfdispbtn")

    def fontsize():
        # 字体大小（2026-10-10）：每个用户每台电脑自己设置，跟账号无关不上云。
        # 走 js_click 隐藏按钮 fontbtn（同 selfdispbtn 的可靠链路）
        js_click("fontbtn")

    def exp_img():
        if not guarded("导出我补的图", "addimg"):
            return
        js_click("expimg")

    def imp_img():
        if not guarded("导入补图", "addimg"):
            return
        js_click("impimg")

    def autostart_toggle():
        # 开机自启开关（2026-10-25）：Mac 没有托盘，入口放原生菜单；Windows 菜单/托盘都有。
        # 关 = 删配置 + 写 autostart_off.txt 标记，下次启动不再自动开回来。
        autostart_turn(not autostart_state(), state.LOG.append)
        try:
            webview.windows[0].evaluate_js(
                "alert('开机自启现在：%s。')" % ("已开启" if autostart_state() else "已关闭"))
        except Exception:
            pass

    autostart_lab = "开机自启（当前：%s）" % ("已开启" if autostart_state() else "已关闭")

    # 子账号（2026-10-08 十次改版）：菜单 = 账号管理 / 显示 / 重新加载页面 / 软件更新 / 退出登录
    # 2026-10-10 加「显示(S)」：字体大小每个用户每台电脑自己调（跟账号无关、不上云）
    if sub:
        return [
            MenuAction("账号管理", adm),
            Menu("显示(&S)", [
                MenuAction("字体大小", fontsize),
            ]),
            MenuAction("重新加载页面", reload_page),
            MenuAction("软件更新", upd_app),
            MenuAction("退出登录", logout),
        ]
    # 2026-10-08 顶部菜单归类（用户要求：平铺 9 项太乱，收成三个下拉）：
    #   数据(D) = 页面/在线表/数据更新/补图导入导出；账号(A) = 账号管理/退出登录；帮助(H) = 软件更新/关于
    return [
        Menu("数据(&D)", [
            MenuAction("全部更新", upd_all),
            MenuAction("重新加载页面", reload_page),
            MenuSeparator(),
            MenuAction("从金山文档更新数据", upd),
            MenuAction("更新实时库存", upd_stock),
            MenuSeparator(),
            MenuAction("打开在线表（公布版）", source),
            MenuAction("打开在线表（价格表）", source_price),
            MenuSeparator(),
            MenuAction("导出我补的图", exp_img),
            MenuAction("导入补图", imp_img),
        ]),
        Menu("账号(&A)", [
            MenuAction("账号管理", adm),
            MenuAction("退出登录", logout),
        ]),
        Menu("显示(&S)", [
            MenuAction("我的显示开关", selfdisp),
            MenuSeparator(),
            MenuAction("字体大小", fontsize),
        ]),
        Menu("帮助(&H)", [
            MenuAction("软件更新", upd_app),
            MenuSeparator(),
            MenuAction(autostart_lab, autostart_toggle),
            MenuAction("关于", about),
        ]),
    ]


def _built_desc():
    try:
        p = get_payload()
        return "%d 个型号 / %d 张机图 · 数据 %s" % (
            len(p["rows"]), len(p.get("imgs") or {}), (p.get("meta") or {}).get("built"))
    except Exception:
        return "数据未就绪"


def auto_updater():
    """云端库存跟随线程（2026-10-09 v2.8 全云端口径）：
    本机每小时直拉金山/用友的自动更新已全部取消 —— 定时更新全在云端
    （GitHub Actions，北京时间 8:00-24:00 每小时，凌晨不跑）。
    本机只做 60 秒轻探：云端有比本地新的库存就自动同步下来，所有角色统一走这条；
    金山表格的跟随在 xlsx_follower（也是 60 秒）。软件活着就在跟，托盘/最小化不影响。"""
    time.sleep(180)                      # 启动头几分钟让位给首屏加载
    while True:
        try:
            if read_state()["status"] not in ("running", "login"):
                stock_pull_cloud(state.LOG.append, force=False)
        except Exception as e:
            state.LOG.append("自动同步（云端库存）异常：%s" % e)
        time.sleep(60)


def autostart_enable(log=None):
    """开机自启（HKCU Run 键，只对当前用户，无需管理员）。
    已存在同名项就不重复写。失败只记日志，不影响启动。"""
    try:
        import winreg
        if os.path.isfile(wp("autostart_off.txt")):
            return True          # 2026-10-25：用户关过自启，别再自动开回来
        if is_frozen():
            exe = os.path.abspath(sys.argv[0])          # 打包后 = ModelLookup.exe 本体
        else:
            exe = sys.executable                        # dev 调试用 python.exe
        val = '"%s"' % exe
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            try:
                old, _ = winreg.QueryValueEx(k, APP_NAME)
                if old == val:
                    return True          # 已配置好
            except FileNotFoundError:
                pass
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, val)
        if log:
            log("已设置开机自启：%s" % val)
        return True
    except Exception as e:
        if log:
            log("开机自启设置失败（不影响使用）：%s" % e)
        return False


def autostart_state():
    """当前是否已设置开机自启（跨平台：Mac 走 LaunchAgent，Windows 走 HKCU Run 键）。"""
    if sys.platform == "darwin":
        import appwin_mac
        return appwin_mac.autostart_enabled()
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as k:
            winreg.QueryValueEx(k, APP_NAME)
        return True
    except Exception:
        return False


def autostart_turn(on, log=None):
    """跨平台开/关开机自启（2026-10-25，修「托盘关了下次启动又自动开回来」）。
    关 = 删自启配置 + 写 autostart_off.txt 标记（启动线程看到标记就不再强开）；
    开 = 删标记 + 重新写配置。"""
    off = wp("autostart_off.txt")
    if on:
        try:
            os.remove(off)
        except OSError:
            pass
        return autostart_enable(log)
    try:
        open(off, "w").close()
    except Exception:
        pass
    if sys.platform == "darwin":
        import appwin_mac
        return appwin_mac.autostart_remove(log)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, APP_NAME)
        if log:
            log("已关闭开机自启")
        return True
    except FileNotFoundError:
        return True
    except Exception as e:
        if log:
            log("关闭开机自启失败：%s" % e)
        return False


def warn_no_webview2():
    """没有 WebView2 运行时 → 用原生 MessageBox 说清楚，别让用户干瞪眼。"""
    msg = ("本机缺少 Microsoft Edge WebView2 运行时，程序没法显示界面。\n\n"
           "装一下就好（免费，2MB）：\n"
           "https://go.microsoft.com/fwlink/p/?LinkId=2124703\n\n"
           "装完重新打开本程序即可。")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, APP_NAME, 0x10)
    except Exception:
        pass


def yy_alert_loop():
    """管理号专用后台线程：每 60 秒轮询云端用友告警（子账号机器不查）。
    告警由别的机器的用友自动登录失败产生 → 这里收到后存 STATE，页面亮横幅。
    挂在 auto_updater 之前启动（登录前 PERM_STATE.role 还是 None，进了循环
    每轮现读，登录后下一轮自动生效）。"""
    time.sleep(90)                       # 启动缓一缓，等登录态就位
    while True:
        try:
            yy_alert_poll()
        except Exception:
            pass
        time.sleep(60)


def run_app(httpd, port, startup_xlsx=None):
    url = "http://%s:%d/" % (HOST, port)

    class _JsBridge:
        """页面 → Python 的桥：Ctrl+W 在 WebView2 里拦不到（键盘直达 Chromium 子窗口，
        WinForms KeyPreview 无效），由页面 keydown 调这里的 hide_main() 收进托盘。"""
        def hide_main(self):
            try:
                winref.MAIN_WIN.hide()
            except Exception:
                pass

        def pick_folder(self):
            """系统选文件夹对话框（2026-10-24：素材存储路径设置用）。取消返回空串。"""
            try:
                res = winref.MAIN_WIN.create_file_dialog(webview.FOLDER_DIALOG)
                return str(res[0]) if res else ""
            except Exception:
                return ""

    _dpi()                                  # 必须在量屏幕 / 建窗前：否则算出来是缩放前的尺寸
    w, h, x, y = fit_window(1480, 940)
    lw, lh, lx, ly = fit_window(1180, 860)

    t_cw = time.time()
    winref.MAIN_WIN = webview.create_window(
        APP_NAME,
        url=url,
        width=w, height=h, x=x, y=y,
        # maximized 不开（2026-10-13 用户改口：打开不要默认全屏，用记忆尺寸即可）
        min_size=(900, 600),
        background_color="#f5f7fb",
        text_select=True,
        menu=build_menu(),
        js_api=_JsBridge(),
    )
    _tick("主窗口对象建好（%.0f ms）" % ((time.time() - t_cw) * 1000))
    t_cw = time.time()
    # 登录窗口：先建好藏着，需要扫码时才 show()（创建必须发生在 start() 之前）
    winref.LOGIN_WIN = webview.create_window(
        "登录金山文档 · 扫码后不用点任何按钮",
        html="<body style='background:#fff;font:14px \"Microsoft YaHei\";padding:30px;color:#5b6577'>"
             "正在打开金山文档…</body>",
        width=lw, height=lh, x=lx, y=ly, min_size=(700, 520), hidden=True,
    )
    _tick("登录窗口对象建好（%.0f ms）" % ((time.time() - t_cw) * 1000))
    # 定时自动更新（金山价格 + 用友库存，每小时一轮，静默）
    threading.Thread(target=auto_updater, daemon=True).start()
    # 云端表格跟随（GitHub Actions 定时拉金山传云端 → 本机 60 秒轻探自动重建）
    threading.Thread(target=xlsx_follower, daemon=True).start()
    threading.Thread(target=xinsheng_follower, daemon=True).start()   # 欣盛商城月度同步（官方售价/补图）
    threading.Thread(target=yy_alert_loop, daemon=True).start()   # 管理号轮询用友告警

    def on_login_closed():
        winref.LOGIN_CLOSED.set()

    try:
        winref.LOGIN_WIN.events.closed += on_login_closed
    except Exception:
        pass

    # 用友窗口：同样先建好藏着，点「更新实时库存」需要登录时才 show()
    winref.YY_WIN = webview.create_window(
        "用友登录 · 登录一次，之后点更新直接拉实时库存",
        html="<body style='background:#fff;font:14px \"Microsoft YaHei\";padding:30px;color:#5b6577'>"
             "正在打开用友 YonSuite…</body>",
        width=520, height=680, hidden=True,
    )
    try:
        winref.YY_WIN.events.closed += lambda: winref.YY_CLOSED.set()
    except Exception:
        pass

    def on_main_closed():
        set_state("error", "窗口已关闭")
        try:
            if winref.LOGIN_WIN is not None and not winref.LOGIN_CLOSED.is_set():
                winref.LOGIN_WIN.destroy()
        except Exception:
            pass
        try:
            if winref.YY_WIN is not None and not winref.YY_CLOSED.is_set():
                winref.YY_WIN.destroy()
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

    _REAL_EXIT = [False]        # 真·退出标志（托盘「退出」才置 True）

    def setup_tray():
        """托盘：点 × / Ctrl+W = 收进托盘不退出；托盘双击恢复；托盘菜单「退出」真退。
        winforms 层直接挂 FormClosing / KeyDown（KeyPreview 让 Ctrl+W 先于 WebView2 被拦下）。
        坑（2026-10-09 修）：native 窗口是 GUI 线程异步建的，after_start 一进来就取十有八九
        还是 None —— 旧代码取不到就直接跳过，等于托盘从没装上、点 × 直接真退出。
        现在后台轮询等 native 出现，再 BeginInvoke 到 UI 线程装托盘。"""
        def _late():
            for _ in range(150):                 # 最多等 15 秒
                bv = getattr(winref.MAIN_WIN, "native", None)
                if bv is not None:
                    try:
                        import System.Windows.Forms as _WF
                        bv.BeginInvoke(_WF.MethodInvoker(lambda: _wire_tray(bv)))
                    except Exception:
                        try:
                            _wire_tray(bv)       # BeginInvoke 不行就硬调（总比不装强）
                        except Exception as e:
                            _tick("托盘迟到安装失败: %r" % e)
                    return
                time.sleep(0.1)
            _tick("托盘：始终没拿到原生窗口，放弃")
        threading.Thread(target=_late, daemon=True).start()

    def _wire_tray(bv):
        try:
            import System.Windows.Forms as WF
            import System.Drawing as DG
            ico_path = rp("icon.ico")
            ic = DG.Icon(ico_path) if os.path.isfile(ico_path) else DG.SystemIcons.Application

            ni = WF.NotifyIcon()
            ni.Icon = ic
            ni.Text = "型号查询（双击打开）"
            ni.Visible = True

            def _show(sender=None, e=None):
                try:
                    bv.Show()
                    bv.Activate()
                except Exception:
                    pass

            def _real_quit(sender=None, e=None):
                _REAL_EXIT[0] = True
                try:
                    ni.Visible = False
                    ni.Dispose()
                except Exception:
                    pass
                try:
                    httpd.shutdown()
                except Exception:
                    pass
                # 先优雅关窗（给 WebView2 半秒把 localStorage 落盘，登录态才保得住），再兜底硬退
                try:
                    bv.Close()
                except Exception:
                    pass
                time.sleep(0.8)
                os._exit(0)          # 后台线程（自动更新等）一并干净走人

            def _autostart_toggle(sender=None, e=None):
                """托盘菜单「开机自启」勾选开关。
                2026-10-25：关 = 删 Run 键 + 写 autostart_off.txt 标记（下次启动不再自动开回来）。"""
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as k:
                        winreg.QueryValueEx(k, APP_NAME)
                    has = True
                except FileNotFoundError:
                    has = False
                except Exception:
                    has = False
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                        if has:
                            winreg.DeleteValue(k, APP_NAME)
                            try:
                                open(wp("autostart_off.txt"), "w").close()
                            except Exception:
                                pass
                        else:
                            try:
                                os.remove(wp("autostart_off.txt"))
                            except OSError:
                                pass
                            exe = os.path.abspath(sys.argv[0]) if is_frozen() else sys.executable
                            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, '"%s"' % exe)
                    mi3.Checked = not has
                except Exception:
                    pass

            menu = WF.ContextMenuStrip()
            mi1 = WF.ToolStripMenuItem("打开 型号查询")
            mi1.Click += _show
            mi3 = WF.ToolStripMenuItem("开机自启")
            mi3.CheckOnClick = True
            try:
                import winreg as _wr
                with _wr.OpenKey(_wr.HKEY_CURRENT_USER, RUN_KEY, 0, _wr.KEY_QUERY_VALUE) as k:
                    _wr.QueryValueEx(k, APP_NAME)
                mi3.Checked = not os.path.isfile(wp("autostart_off.txt"))
            except Exception:
                mi3.Checked = False
            mi3.Click += _autostart_toggle
            mi2 = WF.ToolStripMenuItem("退出")
            mi2.Click += _real_quit
            menu.Items.Add(mi1)
            menu.Items.Add(mi3)
            menu.Items.Add(mi2)
            ni.ContextMenuStrip = menu
            ni.DoubleClick += _show

            tipped = [False]

            def _hide_to_tray():
                try:
                    bv.Hide()
                except Exception:
                    pass
                if not tipped[0]:
                    tipped[0] = True
                    try:
                        ni.BalloonTipTitle = "型号查询还在运行"
                        ni.BalloonTipText = "已收进托盘：双击托盘图标打开。点 × 或 Ctrl+W 都只是收起来，退出请用托盘右键「退出」。"
                        ni.ShowBalloonTip(3000)
                    except Exception:
                        pass

            def _on_closing(sender, e):
                if _REAL_EXIT[0]:
                    return
                e.Cancel = True
                _hide_to_tray()

            def _on_keydown(sender, e):
                try:
                    if e.Control and e.KeyCode == WF.Keys.W:
                        e.Handled = True
                        e.SuppressKeyPress = True
                        _hide_to_tray()
                except Exception:
                    pass

            bv.FormClosing += _on_closing
            bv.KeyPreview = True
            bv.KeyDown += _on_keydown
            _tick("托盘就绪：× / Ctrl+W 收进托盘，托盘双击恢复，右键退出")
            # 2026-10-19：僵尸绕过启动时告知用户（否则静默双实例互踩数据没人知道）
            try:
                from instance import BYPASSED
                if BYPASSED[0]:
                    ni.BalloonTipTitle = "型号查询：检测到无响应的旧实例"
                    ni.BalloonTipText = ("已有一个无响应的旧实例在后台（端口被占）。本次以独立模式启动，"
                                         "两份数据可能互相覆盖；建议关机重启一次清掉旧实例。")
                    ni.ShowBalloonTip(8000)
                    state.LOG.append("⚠ 本次为僵尸绕过启动（旧实例无响应），建议重启电脑清理")
            except Exception:
                pass
        except Exception as e:
            _tick("托盘初始化失败（不影响其他功能）: %r" % e)

    def after_start():
        _tick("webview.start() 的 GUI 循环已起来")
        state.LOG.append(lambda m: print("[%s] %s" % (APP_NAME, m)))
        state.LOG.append(lambda m: None)
        setup_tray()
        time.sleep(1.5)                      # 等 GUI 把初始菜单建完
        _wire_menu_autoclose()
        # 开机自启（2026-10-08）：管理号机器要常驻后台每小时推库存，子账号要自动跟随云端。
        # 全默认开启；托盘菜单里有「取消开机自启」可以关。
        threading.Thread(target=autostart_enable, args=(state.LOG.append,), daemon=True).start()
        if startup_xlsx:
            time.sleep(1.0)
            do_update(state.LOG.append, xlsx_path=startup_xlsx)

    _tick("准备调 webview.start()")
    # 运维口子：设 MODELLOOKUP_CDP_PORT=9333 再启动，可远程调试页面
    # （_kdauth.py 抓金山登录态就靠它；pywebview 走 settings 传给 WebView2 —— 2026-10-09）
    cdp_port = (os.environ.get("MODELLOOKUP_CDP_PORT") or "").strip()
    if cdp_port.isdigit():
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(cdp_port)
    webview.start(after_start, gui="edgechromium", debug=False,
                  private_mode=False, storage_path=wp("webview"),
                  http_server=False)
