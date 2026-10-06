# -*- mode: python ; coding: utf-8 -*-
"""型号查询 exe 打包配置（PyInstaller）。

构建：
  python -m PyInstaller --noconfirm --distpath dist_v1 --workpath build_v1 ModelLookup.spec

要点：
  · openpyxl / webview 必须是顶层 import，否则 PyInstaller 判为「延迟可选」不打包
  · webview 的 WebView2 DLL 由 hooks-contrib 的 hook-webview 自动收进去
  · 绝不把 cookies.json 打进包里（那是本机登录态，装到别人电脑上没意义还泄露）
"""
import os

HERE = os.path.abspath(os.path.dirname(SPEC))          # noqa: F821
A = os.path.join(HERE, "assets")

datas = [
    (os.path.join(A, "template.html"), "assets"),
    (os.path.join(A, "payload.json"), "assets"),
    (os.path.join(A, "images_cache.json"), "assets"),
    (os.path.join(A, "icon.ico"), "assets"),
]

a = Analysis(
    ["main.py"],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "openpyxl", "et_xmlfile",
        "webview", "webview.menu", "webview.platforms.winforms",
        "clr", "pythonnet", "clr_loader",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 图像是预先烤好的 dataURL，本工具不跑 numpy/PIL/Qt
    excludes=["numpy", "scipy", "pandas", "matplotlib", "PIL", "Pillow",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pytest",
              "setuptools", "pkg_resources", "wheel",
              "tkinter", "unittest", "pydoc_data",
              # 其它 pywebview 后端一律不要（Windows 只用 edgechromium/winforms）
              # ⚠ 千万别排除 webview.platforms.win32：winforms 顶层就 `from webview.platforms import win32`，
              #   排掉它会让 import winforms 直接失败，pywebview 会报成「You must have pythonnet installed」
              "webview.platforms.cef", "webview.platforms.gtk",
              "webview.platforms.qt", "webview.platforms.android",
              "webview.platforms.cocoa", "webview.platforms.mshtml"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ModelLookup",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                 # 独立窗口应用，不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(A, "icon.ico"),
)
