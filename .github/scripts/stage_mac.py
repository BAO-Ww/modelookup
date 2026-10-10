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
    subprocess.run(["hdiutil", "create", "-volname", "ModelLookup",
                    "-srcfolder", app, "-ov", "-format", "UDZO", dmg],
                   check=True)

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
