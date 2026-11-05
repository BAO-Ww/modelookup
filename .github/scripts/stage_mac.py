# -*- coding: utf-8 -*-
"""CI（macOS runner）里把 Mac 构建产物整理到 out/。
2026-10-11 Mac 端新增，与 stage.py（Windows）平行。

产物（ASCII 名，GitHub Releases 只收 ASCII 文件名）：
  out/ModelLookup-mac-<版本>.dmg   —— 首次安装用（拖拽到 Applications）
  out/ModelLookup-mac-<版本>.zip   —— 软件自更新用（ditto 打包，保留 .app 结构与权限）

用法: python .github/scripts/stage_mac.py <版本号，如 2.7.25>
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))


def main():
    ver = (sys.argv[1] if len(sys.argv) > 1 else "").strip() or "dev"

    app = os.path.join(ROOT, "app", "dist", "ModelLookup.app")
    if not os.path.isdir(app):
        raise SystemExit("找不到构建产物：%s" % app)

    out = os.path.join(ROOT, "out")
    if os.path.isdir(out):
        shutil.rmtree(out)
    os.makedirs(out, exist_ok=True)

    dmg = os.path.join(out, "ModelLookup-mac-%s.dmg" % ver)
    # 2026-11-05：dmg 里除 .app 外再放两样东西 ——
    #   ① 修复并打开.command：一键去隔离 + 搬出包内误建的「图片库」+ 重签 + 打开
    #      （用户「完全没有反应，窗口都没有」的现场急救；App 里是没法自救的，
    #        因为签名一破 macOS 连 Python 都不让起来）
    #   ② 安装说明.txt：Control+点击打开、图片存哪、被拦的原因
    # 打包目录用 ditto 复制（保住 .app 的符号链接/xattr/权限，cp -r 会改动签名相关元数据）
    stage = os.path.join(out, "_dmg")
    os.makedirs(stage, exist_ok=True)
    subprocess.run(["ditto", app, os.path.join(stage, "ModelLookup.app")], check=True)
    here = os.path.dirname(os.path.abspath(__file__))
    for src_name, dst_name in (("mac_repair.command", "修复并打开.command"),
                               ("mac_README.txt", "安装说明.txt")):
        src = os.path.join(here, src_name)
        if os.path.isfile(src):
            dst = os.path.join(stage, dst_name)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o755)
        else:
            print("  ⚠ 缺分发文件：%s（dmg 里不会有 %s）" % (src, dst_name))
    subprocess.run(["hdiutil", "create", "-volname", "ModelLookup",
                    "-srcfolder", stage, "-ov", "-format", "UDZO", dmg],
                   check=True)
    shutil.rmtree(stage, ignore_errors=True)

    zipf = os.path.join(out, "ModelLookup-mac-%s.zip" % ver)
    # ditto 保留 .app 目录结构与可执行权限；--keepParent 让 zip 里顶层就是 ModelLookup.app
    subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                    app, zipf], check=True)

    for f in (dmg, zipf):
        print("  %-34s %10.2f MB" % (os.path.basename(f),
                                     os.path.getsize(f) / 1048576.0))
    for need in (dmg, zipf):
        if not os.path.isfile(need) or os.path.getsize(need) < 1048576:
            raise SystemExit("必需的产物缺失或太小：" + need)
    print("out ready: %s" % out)


if __name__ == "__main__":
    main()
