# -*- coding: utf-8 -*-
"""本地服务 POST 路由（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
ApiPostMixin.do_POST——_send 经 MRO 复用 ApiGetMixin 的。
依赖方向：server 顶层（这里触到 appwin.rebuild_menu_for，方向 server→appwin 合法）。"""
import base64
import json
import os
import threading
import urllib.parse

import state
import winref
from appwin import rebuild_menu_for
from cloud import stock_pull_cloud
from paths import wp
from state import read_state, set_state
from updater import _app_install_bg, _updateall_bg, do_update


class ApiPostMixin:

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/perm":
            # 页面登录后把权限表报上来（原生菜单点按时校验）
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                state.PERM_STATE["u"] = d.get("u")
                state.PERM_STATE["role"] = d.get("role")
                state.PERM_STATE["perm"] = d.get("perm") or {}
                state.LOG.append("权限已同步：%s（%s）" % (
                    d.get("u") or "?", "管理号" if d.get("role") == "admin" else "子账号"))
                # 2026-10-09 用户要求：标题栏显示当前登录账号
                try:
                    _u = str(d.get("u") or "")
                    _r = "管理号" if d.get("role") == "admin" else "子账号"
                    if winref.MAIN_WIN is not None:
                        winref.MAIN_WIN.set_title("型号查询 — %s（%s）" % (_u, _r))
                except Exception:
                    pass
                # 按角色重画原生菜单：子账号只留 账号管理/退出登录
                threading.Thread(target=rebuild_menu_for,
                                 args=(d.get("role"),), daemon=True).start()
            except Exception as e:
                state.LOG.append("权限同步失败：%s" % e)
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        if u.path == "/api/appinstall":
            # 一键更新：后台线程下安装包 → 静默覆盖装 → 本进程退出
            if read_state()["status"] in ("running", "login"):
                self._send(200, json.dumps({"ok": False, "msg": "数据更新进行中，等它跑完再更新软件"},
                                           ensure_ascii=False))
                return
            set_state("running", "正在下载新版本安装包…")
            threading.Thread(target=_app_install_bg, args=(state.LOG.append,), daemon=True).start()
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        if u.path == "/api/token":
            # 登录态镜像（写）：登录时存 token JSON、退出登录时清掉。文件在 wp() 目录。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                f = wp("auth_token.json")
                t = (d.get("token") or "").strip()
                if t:
                    with open(f, "w", encoding="utf-8") as fh:
                        fh.write(t)
                elif os.path.isfile(f):
                    os.remove(f)
            except Exception as e:
                state.LOG.append("登录态镜像失败：%s" % e)
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        if u.path == "/api/stock":
            # 2026-10-10 全云端：管理号→触发云端拉用友（同「全部更新」的 yy 分支）；
            # 子账号→直接从云端同步。本机 CAS 直拉/自动登录已退役（错密码撞锁号风险）。
            if read_state()["status"] in ("running", "login") or state._ALLRUN[0]:
                self._send(200, json.dumps({"ok": False, "running": state._ALLRUN[0],
                                            "msg": "已有更新在进行中"}, ensure_ascii=False))
                return
            if state.PERM_STATE.get("role") not in (None, "admin"):
                set_state("running", "正在从云端同步库存…")
                threading.Thread(target=stock_pull_cloud, args=(state.LOG.append,),
                                 daemon=True).start()
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
                return
            state._ALLRUN[0] = True
            state._ALLRUN[1] = ""
            state._ALLRUN[2] = ""
            set_state("running", "正在触发云端更新（GitHub Actions）…")
            threading.Thread(target=_updateall_bg, args=(state.LOG.append, "yy"), daemon=True).start()
            self._send(200, json.dumps({"ok": True, "running": True}, ensure_ascii=False))
            return
        if u.path == "/api/updateall":
            # 全部更新（POST 启动，云端版 2026-10-09）：触发 GitHub Actions → 云端跑完自动同步
            wf = "all"
            try:
                n = int(self.headers.get("Content-Length") or 0)
                b = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}") if n else {}
                wf = b.get("wf") if b.get("wf") in ("all", "kd", "yy") else "all"
            except Exception:
                pass
            if read_state()["status"] in ("running", "login") or state._ALLRUN[0]:
                self._send(200, json.dumps({"ok": False, "running": True,
                                            "msg": "已有更新在进行中"}, ensure_ascii=False))
                return
            if state.PERM_STATE.get("role") not in (None, "admin"):
                self._send(200, json.dumps({"ok": False, "msg": "只有管理号能触发云端更新（子账号的数据会自动跟着云端走）"},
                                           ensure_ascii=False))
                return
            state._ALLRUN[0] = True
            state._ALLRUN[1] = ""
            state._ALLRUN[2] = ""
            set_state("running", "正在触发云端更新（GitHub Actions）…")
            threading.Thread(target=_updateall_bg, args=(state.LOG.append, wf), daemon=True).start()
            self._send(200, json.dumps({"ok": True, "running": True}, ensure_ascii=False))
            return
        if u.path != "/api/upload":
            self._send(404, json.dumps({"ok": False, "msg": "not found"}, ensure_ascii=False))
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            # 页面用 base64 传（避免 multipart 解析）
            txt = raw.decode("utf-8", "ignore").strip()
            if txt.startswith("data:"):
                txt = txt.split(",", 1)[-1]
            data = base64.b64decode(txt)
            if data[:2] != b"PK":
                raise RuntimeError("这不是一个 xlsx 文件")
            if read_state()["status"] in ("running", "login"):
                self._send(200, json.dumps({"ok": False, "msg": "正在更新中，请稍候"},
                                           ensure_ascii=False))
                return
            set_state("running", "正在解析上传的 Excel…")
            threading.Thread(target=do_update, args=(state.LOG.append,), kwargs={"xlsx_bytes": data},
                             daemon=True).start()
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
        except Exception as e:
            self._send(200, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
