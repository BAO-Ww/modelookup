# -*- coding: utf-8 -*-
"""
型号查询  v2.7.2
====================================
独立窗口的桌面软件（自带 WebView2 内核，无地址栏 / 无浏览器外壳）：

  · 双击 → 先亮骨架页（页面只有几十 KB），数据随后从本地服务取 → 窗口约 1.5 秒出现
  · 页面上 / 菜单里「从金山文档更新」→ 触发云端（GitHub Actions）拉两个在线表 → 跑完自动同步重建
  · 「更新实时库存」→ 管理号：触发云端拉用友现存量，跑完自动共享给所有子账号；
                        子账号：直接从云端同步管理号共享的库存
      · 2026-10-10 全云端口径：本机不再直连金山/用友（本机拉取入口已删——
        杜绝旧登录态失效、错密码撞锁号这类本机环境问题，所有人看到同一份云端数据）
  · 也支持把从金山文档下载的 xlsx 拖到 exe 图标上重建

数据源（两个）：
  ① 公布版-滞销样机清理明细   分享码 ch7gwGTP9De4 / fileId 548020345856
        只读「10月滞销清理明细」「10月样机清理明细」两个 sheet（用户明确要求，别的不读）
  ② 2026年10月价格表-LYDF-共享  分享码 cficbK9Drrdp（fileId 运行时探测后缓存）
        只读 sheet「型号价格表（只看这个）」

自检:  型号查询.exe --check        （结果写到 exe 同目录 exe_check.log）

2026-10-11 拆分一期：本文件只剩 入口三件事 ——
  ① _PYI 环境清洗（必须在第一行逻辑，拆出去自更新链会炸：GHSA-9fxf 坑）
  ② main() 启动分发（单实例检查 → 骨架页 → 本地服务 → 窗口）
  ③ --check 自检（CI 拿 RESULT: PASS/FAIL 哨兵判成败）
其余按职责域住在同级模块（依赖方向单向，见拆分设计.md）：
  config → paths → state → normalize → parsing(xlsx_core/parsers_haier/parsers_yr)
  → payload → net → cloud → kdocs/yyclient(yy_transport/yy_agg/yy_login) → updater
  → server(api_get/api_post/instance) → appwin → main
"""
import io
import json
import os
import sys
import traceback
import urllib.parse
import urllib.request

# ⚠ 顶层 import（不能放进函数里）：放进函数体内会被 PyInstaller 判定为「延迟/可选」而不打包
import openpyxl  # noqa: E402
import webview  # noqa: E402

# 2026-10-10 修「Security validation failure: parent process has different
# executable!」：PyInstaller 6.22.1 起引导器给 onefile 加了父进程安全校验
# （GHSA-9fxf-4qw3-ghmr）——进程环境里若带着 _PYI_* 引导变量，引导器就认定
# 自己是「onefile 子进程」，要求父进程必须跟自己是同一个 exe，否则弹
# Security validation failure 直接退出（父进程拿不到路径时弹
# "failed to obtain executable path for parent process"）。
# 坑点：本程序就是 onefile exe，运行环境里正带着 _PYI_*；自更新链
# cmd 帮助进程 → Setup 安装器 → 拉起新版会把这些变量一路继承下去，
# 新版引导器因此误判 → 更新完新版永远弹错起不来（2.7.13→2.7.14 实测中招）。
# 处理：启动最前面把 _PYI_*/_MEIPASS2 从本进程环境删掉（老版 PyInstaller
# 时代防 _MEIPASS2 污染子进程的标准做法），此后所有子进程拿到的都是干净环境。
for _k in [k for k in os.environ if k.startswith("_PYI") or k == "_MEIPASS2"]:
    try:
        del os.environ[_k]
    except Exception:
        pass
# 2026-10-10：del _k 在「环境里本来就没有 _PYI_*」时会 NameError（源码直跑实锤）——
# 循环体没执行 _k 就没定义。改成兜底赋值再删，两条路都安全。
_k = None
del _k

# 2026-10-11 拆分一期：常量/路径/归一化/解析/payload/网络/云端/金山/用友/更新/服务/窗口
# 全部住在同级模块（纯搬运，行为零变化；验证协议见 baseline/_cmp_payload.py）。
from config import APP_NAME, APP_VERSION, HOST                            # noqa: E402
from paths import is_frozen, res_dir, work_dir, rp, wp                    # noqa: E402
from normalize import NOT_BRAND                                           # noqa: E402
from state import _tick, price_load, stock_load                           # noqa: E402
from payload import bootstrap, get_payload                                # noqa: E402
from server import start_server                                           # noqa: E402
from instance import (activate_existing, already_running,                 # noqa: E402
                      any_live_instance, read_port, save_port, BYPASSED)  # noqa: E402
from appwin import _webview2_pv, warn_no_webview2                         # noqa: E402
# 2026-10-11 Mac 端：窗口域按平台分发（Windows 走 appwin 不变，Mac 走 appwin_mac）
if sys.platform == "darwin":
    from appwin_mac import run_app                                        # noqa: E402
else:
    from appwin import run_app                                            # noqa: E402

# 调试开关：WebView2 远程调试口（CDP）——goofish 内嵌页自动化依赖它
# （2026-10-17：「快速进入」选号页自动点 + 菜单真实路由探测，见 xy_cdp.py）。
# 默认开启，端口 pid 派生防多进程撞口；外部用 ML_CDP_PORT 可指定。
try:
    _dbg_port = int(os.environ.get("ML_CDP_PORT") or 0)
    if not _dbg_port:
        _dbg_port = 9333 + (os.getpid() % 300)
        os.environ["ML_CDP_PORT"] = str(_dbg_port)
    if sys.platform != "darwin":            # WKWebView 无此 setting
        webview.settings["REMOTE_DEBUGGING_PORT"] = _dbg_port
except Exception:
    pass

_tick("常量区/埋点就绪")


def _wipe_stale_local():
    """2026-10-15 用户定稿：版本更新后第一次启动，清掉会串版本的旧状态再起。
    清：固化页 + 各同步时间戳（ts 没了 → 启动后自动从云端重拉，数据缓存自动重建）。
    不清：payload.json / stock_live.json / xinsheng.sqlite3 —— 它们是数据缓存，
    有自己的版本/新鲜度校验；删了软件会回退到 exe 内嵌老数据（2.7.54 实锤：
    型号数据从 10-15 倒退回 10-07），比不清危害大得多。
    保留：凭据（auth_token/xy_cookies/glm_key）、文件ID（*_fileid.txt）、xlsx 大文件、webview 登录态。"""
    verf = wp("ver.txt")
    try:
        last = io.open(verf, encoding="utf-8").read().strip() if os.path.isfile(verf) else ""
    except Exception:
        last = ""
    if last == APP_VERSION:
        return
    wiped = []
    for _name in ["index.html", "xlsx_ts.txt", "price_ts.txt", "xs_ts.txt"]:
        _p = wp(_name)
        if os.path.isfile(_p):
            try:
                os.remove(_p)
                wiped.append(_name)
            except Exception:
                pass
    try:
        io.open(verf, "w", encoding="utf-8").write(APP_VERSION)
    except Exception:
        pass
    if wiped:
        print("[版本清场] %s -> %s，已清：%s" % (last or "(无记录)", APP_VERSION, ", ".join(wiped)))


def _goofish_mode():
    """--goofish <账号> <url>（2026-10-17）：独立小窗口打开闲鱼页（编辑/查看宝贝内嵌）。
    一号一个 WebView2 档案目录（xyprofile_<账号>），登录态互不干扰、多号可同时开；
    首次扫码登录一次，之后免登。必须在单实例检查之前分支返回（不占端口、不碰主窗口）。"""
    if "--goofish" not in sys.argv:
        return False
    try:
        _i = sys.argv.index("--goofish")
        _acc = sys.argv[_i + 1]
        _url = sys.argv[_i + 2]
    except (IndexError, ValueError):
        return False
    _safe = "".join(ch for ch in _acc if ch.isalnum() or ch in "-_") or "default"
    _sp = wp("xyprofile_" + _safe)
    os.makedirs(_sp, exist_ok=True)
    # 独立窗口同样开 CDP + 自动点「快速进入」（跟主进程同款）。
    # 端口必须重算：独立窗口是子进程，会继承父进程的 ML_CDP_PORT（已绑），不重算就没口
    try:
        import xy_cdp
        os.environ["ML_CDP_PORT"] = str(9333 + (os.getpid() % 300))
        webview.settings["REMOTE_DEBUGGING_PORT"] = int(os.environ["ML_CDP_PORT"])
        xy_cdp.start_autoclick()
    except Exception:
        pass
    try:
        webview.create_window("闲鱼 · %s" % (_acc if _acc != "default" else "宝贝页"), _url,
                              width=1360, height=900)
        webview.start(private_mode=False, storage_path=_sp,
                      gui="edgechromium" if sys.platform != "darwin" else None)
    except Exception as e:
        print("[goofish] 窗口失败：%s" % e)
    return True


def main():
    _tick("main() 进入（Python 侧已起来）")
    # 2026-10-17：--goofish 内嵌闲鱼窗口最先分支（不走向主流程：不清场、不抢单实例）
    if _goofish_mode():
        return
    # 2026-10-15：版本变了先清本地缓存（固化页/库存缓存/payload），再走正常启动
    try:
        _wipe_stale_local()
    except Exception:
        pass
    _tick("版本清场检查完成")
    # Mac 用 WKWebView，没有 WebView2 这回事（2026-10-11 Mac 端）
    if sys.platform != "darwin" and not webview2_ok():
        warn_no_webview2()
        return

    # 已经在跑 → 把那个窗口叫到前面来，绝不开第二个
    # （开第二个的话端口/存储路径会分家，用户标的「已发布」「我的定价」就看不见了）
    # 例外（2026-10-10）：占着互斥量的实例**叫不活**（登记端口没应答、逐个端口探一遍
    # 也没一个活的）= 僵尸实例 —— 强杀都杀不掉，只能等重启。这种必须绕过互斥量
    # 继续起（自动落到下一个空闲端口），否则软件直接锁死到重启（4804 实锤）。
    if already_running():
        p = read_port()
        if p and activate_existing(p):
            return
        z = any_live_instance()
        if z:
            save_port(z)                       # 活实例在别的端口 → 记下来，下次直接叫它
            activate_existing(z)
            return
        _tick("互斥量被僵尸实例占用（无任何应答），绕过单实例继续启动")
        BYPASSED[0] = True                 # 托盘起来后气泡告知（2026-10-19）
    _tick("单实例检查通过")

    arg = None
    for a in sys.argv[1:]:
        if a.lower().endswith((".xlsx", ".xls")) and os.path.isfile(a):
            arg = a
            break

    bootstrap()
    _tick("骨架页写好")
    price_load()
    stock_load()
    _tick("实时库存缓存：%s" % ("上次更新 " + str(state_stock_ts()) if state_stock_ts() else "还没有"))
    httpd, port = start_server()
    _tick("本地服务已起")
    # goofish 内嵌自动化：「快速进入」选号页替用户自动点（2026-10-17 用户要求）
    try:
        import xy_cdp
        xy_cdp.start_autoclick()
    except Exception:
        pass
    run_app(httpd, port, arg)


def state_stock_ts():
    import state
    return state.STOCK.get("ts")


def webview2_ok():
    return _webview2_pv() is not None


# ------------------------------------------------------------------ 自检

def _check():
    out = []
    out.append("%s v%s 自检" % (APP_NAME, APP_VERSION))
    out.append("frozen = %s" % is_frozen())
    out.append("exe = %s" % sys.executable)
    out.append("资源目录 = %s" % res_dir())
    out.append("工作目录 = %s" % work_dir())
    out.append("WebView2 运行时 = %s %s" % (_webview2_pv() or "无", "✅" if webview2_ok() else "（目标机需要）"))
    # 注意：构建机上没有 WebView2 不算打包问题 —— 那是「运行环境」的事，
    # 程序启动时会用原生弹窗提示用户装。所以这里只警告，不判失败。
    ok = True

    try:
        out.append("pywebview = %s ✅" % (getattr(webview, "__version__", "?"),))
    except Exception as e:
        ok = False
        out.append("pywebview ❌ %r" % (e,))

    # ⚠ 这条必须留着：winforms 顶层硬依赖 webview.platforms.win32，
    #   打包时把它 excludes 掉，pywebview 会误导性地报「You must have pythonnet installed」
    if sys.platform == "darwin":
        # 2026-10-11 Mac 端：后端是 cocoa(WKWebView)，winforms/clr 检查不适用
        try:
            import webview.platforms.cocoa as _ck   # noqa: F401
            out.append("cocoa 后端(WKWebView) = 可导入 ✅")
        except Exception as e:
            ok = False
            out.append("cocoa 后端 ❌ 界面根本起不来！%r" % (e,))
        try:
            import AppKit                            # noqa: F401
            out.append("pyobjc(AppKit) = 可导入 ✅")
        except Exception as e:
            ok = False
            out.append("pyobjc(AppKit) ❌ %r" % (e,))
    else:
        try:
            import webview.platforms.winforms as _wf   # noqa: F401
            out.append("winforms 后端 = 可导入 ✅")
        except Exception as e:
            ok = False
            out.append("winforms 后端 ❌ 界面根本起不来！%r" % (e,))

        for mod in ("clr", "pythonnet"):
            try:
                __import__(mod)
                out.append("%s ✅" % mod)
            except Exception as e:
                ok = False
                out.append("%s ❌ %r" % (mod, e))

    try:
        out.append("openpyxl = %s ✅" % openpyxl.__version__)
    except Exception as e:
        ok = False
        out.append("openpyxl ❌ 缺了会导致没法解析表格！%r" % (e,))

    # 2026-10-15 行级化 + 出厂包不带数据：payload.json 从必带清单移除（数据走云端拉取）
    for name in ("template.html", "images_cache.json",
                 "midea_prices.json", "ronshen_prices.json"):
        p = rp(name)
        if os.path.isfile(p):
            out.append("%s = %.2f MB ✅" % (name, os.path.getsize(p) / 1048576.0))
        else:
            ok = False
            out.append("%s ❌ 打包漏带！" % name)

    if is_frozen() and os.path.isfile(rp("cookies.json")):
        ok = False
        out.append("❌ 打包里带了 cookies.json（本机登录态），不应该外发！")

    try:
        p = get_payload()
        n_img = len(p.get("imgs") or {})
        img_ok = all(isinstance(v, str) and v.startswith("data:image/")
                     for v in (p.get("imgs") or {}).values())
        out.append("数据: 型号 %d 个 / 图 %d 张 / dataURL 格式 %s ✅"
                   % (len(p["rows"]), n_img, "正常" if img_ok else "异常"))
        if p["rows"]:
            # 出厂包带了数据才做数据完整性检查（2026-10-15 起默认不带，数据走云端拉取制）
            if n_img == 0:
                ok = False
                out.append("数据里一张图都没有 ❌")
            n_src = len((p.get("meta") or {}).get("sources") or [])
            out.append("数据源 %d 个 %s" % (n_src, "✅" if n_src >= 2 else "⚠ 只认得 1 个表"))
            if n_src < 2:
                ok = False
            # 价格表字段抽查
            has_p = sum(1 for r in p["rows"] if r.get("pt") is not None)
            out.append("带价格表数据的型号 = %d 个 %s" % (has_p, "✅" if has_p else "❌"))
            if not has_p:
                ok = False
            # 「美的」页并进来了没（另一套报价：拿货价·慧买卖 / 给二批 / 价格取整）
            n_md = sum(1 for r in p["rows"]
                       if any(isinstance(r.get(k), (int, float)) for k in ("mh", "m2", "mr")))
            out.append("美的 / 容声型号 = %d 个 %s" % (n_md, "✅" if n_md else "❌ 美的页没读进来"))
            if not n_md:
                ok = False
            # v24 结构：旧库存字段应已删干净（库存/仓库全部走用友实时接口 stock_live.json）
            n_legacy = sum(1 for r in p["rows"]
                           if r.get("inv") or r.get("pgp") is not None or r.get("psp") is not None)
            out.append("payload 残留旧库存字段(pgp/psp/inv) = %d 个 %s"
                       % (n_legacy, "✅" if not n_legacy else "❌ 还是 v23 旧结构"))
            if n_legacy:
                ok = False
            # 品牌里不该再出现品类名（源表把「科沃斯随手吸」的品牌写成「吸尘器」的那个坑）
            badb = sorted({r.get("b") for r in p["rows"]
                           if r.get("b") and any(w in r["b"] for w in NOT_BRAND)})
            out.append("品牌列残留品类名 = %s %s" % (badb or "无", "✅" if not badb else "❌"))
            if badb:
                ok = False
            # 官网价快照（assets/midea_prices.json + ronshen_prices.json）
            n_mp = sum(1 for r in p["rows"] if isinstance(r.get("mp"), (int, float)))
            out.append("带官网价的型号 = %d 个 %s" % (n_mp, "✅" if n_mp else "⚠ 官网价快照没挂上"))
        else:
            out.append("出厂包不带数据（云端拉取制）：启动后 60 秒同步循环自动拉最新 ✅")
    except Exception as e:
        ok = False
        out.append("数据读取 ❌ %r" % (e,))

    # 关键提速验证：exe 版页面必须是「骨架页」（不含数据），否则又会退回 5 秒启动
    try:
        bootstrap()
        sz = os.path.getsize(wp("index.html")) / 1024.0
        out.append("查询页 = %s（%.0f KB）%s" % (wp("index.html"), sz,
                                             "✅ 骨架页" if sz < 400 else "❌ 又把数据内联进去了"))
        if sz >= 400:
            ok = False
    except Exception as e:
        ok = False
        out.append("生成查询页 ❌ %r" % (e,))

    try:
        httpd, port = start_server()
        base = "http://%s:%d" % (HOST, port)
        with urllib.request.urlopen(base + "/api/ping", timeout=6) as r:
            d = json.loads(r.read().decode("utf-8"))
        out.append("本地服务 = 端口 %d / app=%s ✅" % (port, d.get("app")))

        with urllib.request.urlopen(base + "/api/data", timeout=20) as r:
            raw = r.read()
        j = json.loads(raw.decode("utf-8"))
        out.append("/api/data = 型号 %d 个 / 图片 key %d 个 / %.2f MB ✅"
                   % (len(j.get("rows") or []), len(j.get("imgkeys") or []),
                      len(raw) / 1048576.0))

        if j.get("imgkeys"):
            k = j["imgkeys"][0]
            with urllib.request.urlopen(base + "/img/" + urllib.parse.quote(k), timeout=20) as r:
                b = r.read()
            out.append("/img/<core> 抽查 = %s / %d 字节 ✅" % (k, len(b)))
            if len(b) < 100:
                ok = False
                out.append("/img/<core> 返回的内容不像图片 ❌")
        elif j.get("rows"):
            ok = False
            out.append("/api/data 里没有图片 key ❌")
        else:
            out.append("/api/data 无数据（云端拉取制）：跳过图片抽查 ✅")
        httpd.shutdown()
    except Exception as e:
        ok = False
        out.append("本地服务 ❌ %r" % (e,))

    if is_frozen():
        mb = os.path.getsize(sys.executable) / 1048576.0
        out.append("exe 体积 = %.2f MB" % mb)
        if mb > 90:
            out.append("⚠ 体积偏大，检查是不是把 numpy/PIL 之类拖进来了")

    out.append("判定：自检%s" % ("通过 ✅" if ok else "失败 ❌"))
    # ASCII 哨兵：CI 拿这行判断成败，不受控制台编码影响
    out.append("RESULT: %s" % ("PASS" if ok else "FAIL"))
    txt = "\n".join(out)
    logdir = os.path.dirname(sys.executable) if is_frozen() else os.path.dirname(os.path.abspath(__file__))
    try:
        with io.open(os.path.join(logdir, "exe_check.log"), "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception:
        pass
    try:
        sys.stdout.write(txt + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    return txt


if __name__ == "__main__":
    if "--check" in sys.argv:
        _check()
    else:
        try:
            main()
        except Exception:
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, traceback.format_exc(),
                                                 APP_NAME + " 启动失败", 0x10)
            except Exception:
                pass
            # Mac 端（2026-10-11）：没有 MessageBox，落 crash.log 兜底可查
            if sys.platform == "darwin":
                try:
                    with io.open(wp("crash.log"), "a", encoding="utf-8") as f:
                        f.write(traceback.format_exc() + "\n")
                except Exception:
                    pass
