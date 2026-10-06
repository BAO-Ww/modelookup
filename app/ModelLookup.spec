# -*- mode: python ; coding: utf-8 -*-
"""型号查询 exe 打包配置（PyInstaller）。

构建：
  python -m PyInstaller --noconfirm --distpath dist_v1 --workpath build_v1 ModelLookup.spec
"""
import os

HERE = os.path.abspath(os.path.dirname(SPEC))          # noqa: F821
A = os.path.join(HERE, "assets")

datas = [
    (os.path.join(A, "template.html"), "assets"),
    (os.path.join(A, "payload.json"), "assets"),
    (os.path.join(A, "images_cache.json"), "assets"),
    (os.path.join(A, "cookies.json"), "assets"),
    (os.path.join(A, "icon.ico"), "assets"),
]

a = Analysis(
    ["main.py"],
    pathex=[HERE],
    binaries=[],
    datas=datas,
    hiddenimports=["openpyxl", "et_xmlfile"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # PIL / numpy 等一律不要：本工具不处理图片，图像是预先烤好的 dataURL
    excludes=["numpy", "scipy", "pandas", "matplotlib", "PIL", "Pillow",
              "PyQt5", "PyQt6", "PySide2", "PySide6", "IPython", "pytest",
              "setuptools", "pkg_resources", "wheel"],
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
    console=False,                 # GUI 工具，不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(A, "icon.ico"),
)
