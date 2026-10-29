# -*- coding: utf-8 -*-
"""Mac 自更新（2026-10-11 Mac 端新增，与 updater.py 的 Windows 链平行，Windows 代码零改动）。

链路：worker /appcheck?plat=mac → /appdl?tag=X&plat=mac 下 ModelLookup-mac-*.zip →
ditto 解压到 /tmp staging → 独立 bash 帮助进程（detach，不依赖父子关系）：
    等 2 秒 → pkill 旧实例 → 删旧 .app → ditto 新 .app 到位 → xattr 清属性 → open 新版

为什么无签名也能无感更新：Gatekeeper 只拦带 quarantine 隔离属性的文件（浏览器下载
才打）。本更新器程序内 HTTP 下载 + ditto 解压，落盘文件不带 quarantine → 替换后
直接能跑，不弹任何拦截。首次安装（DMG 拖拽）才有 Gatekeeper 提示。
"""
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import state
from net import urlopen_ssl
import winref
from cloud import _cloud_token, cloud_req
from config import APP_ID, CLOUD_HOST, _CLOUD_UA
from paths import is_frozen


def app_bundle_path():
    """当前运行的 .app 包路径（frozen 时）；开发模式返回 None。"""
    if not is_frozen():
        return None
    exe = os.path.abspath(sys.executable)
    marker = os.path.sep + "Contents" + os.path.sep + "MacOS" + os.path.sep
    if marker in exe:
        return exe.split(marker)[0]
    return None


def _app_install_bg(log):
    """软件自更新（Mac 版）：从云端下 zip → 解压校验 → 退出并替换 .app。"""
    try:
        dst_app = app_bundle_path()
        if not dst_app or not os.path.isdir(dst_app):
            state.set_state("error", "开发模式不支持自更新（找不到 .app 包）")
            return
        j, err = cloud_req("/appcheck?plat=mac", timeout=30)
        if err or not j or not j.get("ok"):
            state.set_state("error", "查询新版本失败：%s" % (err or (j or {}).get("msg")))
            return
        tag = str(j.get("tag") or "")
        ver = str(j.get("ver") or "")
        log("自更新(Mac)：开始下载 %s（%.1f MB）…"
            % (j.get("name") or tag, (j.get("size") or 0) / 1048576.0))
        state.set_state("running", "正在下载 Mac 新版 v%s（几十 MB，视网速 1-5 分钟）…" % ver)
        tok = _cloud_token()
        req = urllib.request.Request(CLOUD_HOST + "/appdl?tag="
                                     + urllib.parse.quote(tag) + "&plat=mac")
        req.add_header("X-Token", tok)
        req.add_header("User-Agent", _CLOUD_UA)
        # 更新下载挂节点（2026-10-29 用户令）：settings.json 配了 update_proxy 就走它。
        try:
            from paths import update_proxy as _upd_proxy
            _upx = _upd_proxy()
        except Exception:
            _upx = None
        if _upx:
            log("自更新(Mac)：下载走代理 %s" % _upx)
        work = os.path.join(tempfile.gettempdir(), "modelookup_update")
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work, exist_ok=True)
        zipf = os.path.join(work, "ModelLookup-mac.zip")
        # 并发分块下载（2026-10-29 用户令，与 Windows 端同款）：8 路并发约 5 倍速，
        # 源不支持分段自动退单流，SHA-256 校验兜底完整性。
        from net import dl_parallel as _dlp

        def _pg(got, total):
            if total:
                state.set_state("running",
                                "正在下载新版本 v%s… %d%%" % (ver, got * 100 // total))

        got, total = _dlp(req.get_full_url(), dict(req.headers), zipf,
                          proxy=_upx, progress=_pg, log=log)
        if got < 1048576:
            state.set_state("error", "更新包下载不完整（%d 字节），重试一次" % got)
            return
        # 2026-10-12：appcheck 返回的是 Release 资产真实大小，对账防半截包（64KB 容差）
        exp = int(j.get("size") or 0)
        if exp and abs(got - exp) > 65536:
            state.set_state("error", "更新包不完整（收到 %.1f MB / 应为 %.1f MB），重试一次"
                            % (got / 1048576.0, exp / 1048576.0))
            return
        # 2026-10-14 体检 P0-3：SHA-256 完整性校验（与 Windows 端同款，worker 透传 GitHub digest）
        want_sha = str(j.get("sha256") or "")
        if want_sha:
            h = hashlib.sha256()
            with io.open(zipf, "rb") as f:
                for chunk in iter(lambda: f.read(1048576), b""):
                    h.update(chunk)
            if h.hexdigest() != want_sha:
                shutil.rmtree(work, ignore_errors=True)
                state.set_state("error", "更新包校验失败（SHA-256 不匹配，已丢弃），未安装")
                return
            log("自更新(Mac)：SHA-256 校验通过")
        log("自更新(Mac)：下载完成（%.1f MB），解压校验…" % (got / 1048576.0))
        state.set_state("running", "正在解压新版本…")
        rc = subprocess.run(["/usr/bin/ditto", "-x", "-k", zipf, work],
                            capture_output=True, timeout=300)
        new_app = os.path.join(work, "ModelLookup.app")
        if rc.returncode != 0 or not os.path.isdir(new_app):
            state.set_state("error", "更新包解压失败（zip 里应有 ModelLookup.app）")
            return
        log("自更新(Mac)：新包就绪，退出并替换安装…")
        state.set_state("running", "下载完成，软件将自动关闭并换成新版，装完会自动重新打开…")

        # ---- 帮助脚本（独立 bash，detach；步骤逐条留痕 apply.log，失败可查断点）
        # 2026-10-12 排雷①「先删后装」→「先挪后装」：mv 旧包为 .old（原子改名，
        # app 任何时刻都在磁盘上）→ ditto 新包 → 校验 Contents/MacOS 在位 →
        # 成功才清 .old；ditto 失败则回滚 .old 并照常 open 旧版，更新永不搬空 app。
        sh = os.path.join(work, "apply.sh")

        def _q(p):
            return '"' + p.replace('"', '\\"') + '"'

        old_app = dst_app + ".old"
        lines = [
            "#!/bin/bash",
            "LOG=%s" % _q(os.path.join(work, "apply.log")),
            'echo "start $(date)" > "$LOG"',
            "sleep 2",
            # 杀旧实例（含本进程）。pattern 是 .app 内二进制的路径特征，不会误伤别的程序
            "pkill -f %s; echo \"pkill rc=$?\" >> \"$LOG\""
            % _q(APP_ID + ".app/Contents/MacOS/" + APP_ID),
            "sleep 1",
            # 清掉上一次失败可能残留的备份（只动备份，不碰活着的 app）
            "rm -rf %s; echo \"rmold rc=$?\" >> \"$LOG\"" % _q(old_app),
            # 原子改名让位：旧包整体挪成 .old，此时 app 文件一个没少
            "mv %s %s; echo \"mv rc=$?\" >> \"$LOG\"" % (_q(dst_app), _q(old_app)),
            "ditto %s %s; echo \"ditto rc=$?\" >> \"$LOG\"" % (_q(new_app), _q(dst_app)),
            # 装好判据：新包 Contents/MacOS 目录在位才算成功（显式 / 拼接，不依赖 os.path）
            'if [ -d %s ]; then'
            ' rm -rf %s; echo "install ok, backup cleaned" >> "$LOG";'
            ' else'
            ' rm -rf %s; mv %s %s; echo "ditto FAILED, rolled back to old" >> "$LOG";'
            ' fi'
            % (_q(dst_app + "/Contents/MacOS"),
               _q(old_app),
               _q(dst_app), _q(old_app), _q(dst_app)),
            "xattr -cr %s 2>> \"$LOG\"" % _q(dst_app),
            "/usr/bin/open %s >> \"$LOG\" 2>&1" % _q(dst_app),
            'echo "done $(date)" >> "$LOG"',
        ]
        # 2026-10-12 排雷②：原 ascii+ignore 写法会把路径里的中文静默吞掉，
        # 脚本将操作错误路径。macOS/bash 原生 UTF-8，直接按 UTF-8 写。
        with io.open(sh, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.chmod(sh, 0o755)
        # start_new_session：帮助进程脱离本进程会话，os._exit 后照常存活
        logf = io.open(os.path.join(work, "helper_out.log"), "w")
        subprocess.Popen(["/bin/bash", sh], stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True)
        time.sleep(1.5)                      # 帮助进程先起来，页面此时已收到 ok 回应
        try:
            if winref.MAIN_WIN is not None:
                winref.MAIN_WIN.hide()
        except Exception:
            pass
        os._exit(0)                          # 让位给替换脚本（正常情况已被 pkill 杀掉，兜底）
    except Exception as e:
        state.set_state("error", "自更新失败：%s" % e)
