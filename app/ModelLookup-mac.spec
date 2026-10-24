# -*- mode: python ; coding: utf-8 -*-
"""型号查询 Mac（Apple Silicon）.app 打包配置（PyInstaller，macOS 专用）。
2026-10-11 Mac 端新增，与 ModelLookup.spec（Windows）平行，Windows 打包零影响。

构建（在 macOS 上）：
  python -m PyInstaller --noconfirm --distpath dist --workpath build ModelLookup-mac.spec
  产物：dist/ModelLookup.app（onedir bundle，arm64）

要点：
  · 后端 cocoa(WKWebView)：pyobjc 由 pywebview 在 macOS 上自动带入，hiddenimports 显式点名
  · icon.icns 由 CI 从 assets/icon.png 生成（iconutil）；本机没有 icns 时退 None 也能打
  · 绝不打 winforms/clr/pythonnet（那是 Windows 的）
  · 绝不把 cookies.json 打进包里（本机登录态）
"""
import os

HERE = os.path.abspath(os.path.dirname(SPEC))          # noqa: F821
A = os.path.join(HERE, "assets")
ICNS = os.path.join(A, "icon.icns")

datas = [
    (os.path.join(A, "template.html"), "assets"),
    # 2026-10-15 用户拍板：不再内嵌出厂 payload.json（老数据倒退 10-07 实锤）——
    # 首装/缓存缺失时空载荷占位，60 秒同步循环拉云端重建
    (os.path.join(A, "images_cache.json"), "assets"),
    (os.path.join(A, "midea_prices.json"), "assets"),
    (os.path.join(A, "ronshen_prices.json"), "assets"),
    (os.path.join(A, "stock_rules.json"), "assets"),     # 库存口径单源（2026-10-24）
    # 发布图工厂（2026-10-17 闲鱼配文 Pro 并入）：官网索引 + URL 种子
    (os.path.join(A, "pei", "haier_index.json"), "assets/pei"),
    (os.path.join(A, "pei", "casarte_index.json"), "assets/pei"),
    (os.path.join(A, "pei", "leader_index.json"), "assets/pei"),
    (os.path.join(A, "pei", "xdjdy_index.json"), "assets/pei"),
    (os.path.join(A, "pei", "site_urls_haier.txt"), "assets/pei"),
    (os.path.join(A, "pei", "site_urls_casarte.txt"), "assets/pei"),
    (os.path.join(A, "pei", "site_urls_leader.txt"), "assets/pei"),
    (os.path.join(A, "pei", "site_urls_xdjdy.txt"), "assets/pei"),
]

a = Analysis(
    ["main.py"],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "openpyxl", "et_xmlfile",
        "webview", "webview.menu", "webview.platforms.cocoa",
        # 发布图工厂（2026-10-17 闲鱼配文 Pro 并入）：pei_ai9/pei_hero/pei_core + copywriter 促销首图
        "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont",
        "PIL.ImageFilter", "PIL.ImageStat", "PIL.ImageChops", "PIL.ImageEnhance",
        "websocket",   # goofish 自动化（xy_cdp）：CDP 自动点选号/菜单路由探测（2026-10-17）
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 图像是预先烤好的 dataURL；Windows 后端一律不要
    excludes=["numpy", "scipy", "pandas", "matplotlib",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pytest",
              "setuptools", "pkg_resources", "wheel",
              "tkinter", "unittest", "pydoc_data",
              "clr", "pythonnet", "clr_loader",
              "webview.platforms.winforms", "webview.platforms.edgechromium",
              "webview.platforms.cef", "webview.platforms.gtk",
              "webview.platforms.qt", "webview.platforms.android",
              "webview.platforms.mshtml", "webview.platforms.win32"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：二进制交给 COLLECT
    name="ModelLookup",
    debug=False,
    strip=False,
    upx=False,
    console=False,                  # 独立窗口应用，无终端
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,               # 在 arm64 runner 上原生就是 arm64
    codesign_identity=None,         # 无开发者账号，不签名
    entitlements_file=None,
    icon=ICNS if os.path.isfile(ICNS) else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="ModelLookup",
)

app = BUNDLE(
    coll,
    name="ModelLookup.app",
    icon=ICNS if os.path.isfile(ICNS) else None,
    bundle_identifier="cn.baozang.modelookup",
    info_plist={
        "CFBundleDisplayName": "型号查询",
        "CFBundleName": "ModelLookup",
        "CFBundleShortVersionString": "0.0",   # CI 用 plutil 改成真实版本
        "NSHighResolutionCapable": True,
        # WKWebView 加载 http://127.0.0.1 本地服务，放行 ATS
        "NSAppTransportSecurity": {"NSAllowsArbitraryLoads": True},
        "LSMinimumSystemVersion": "11.0",
    },
)
