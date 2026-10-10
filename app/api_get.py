# -*- coding: utf-8 -*-
"""本地服务 GET 路由（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
ApiGetMixin.do_GET + _send——Handler(ApiGetMixin, ApiPostMixin, BaseHTTPRequestHandler)。
依赖方向：server 在依赖图顶层（Handler 触碰 7 个域，只能放这）。"""
import io
import json
import os
import sys
import threading
import time
import urllib.parse

import state
import winref
from cloud import _ver_gt, cloud_req
from config import APP_ID, APP_VERSION
from paths import wp
from payload import api_data_bytes, get_payload, img_bytes
from state import _tick, read_state, set_state
from updater import _updateall_bg


class ApiGetMixin:
    """GET 路由（self 共享：_send 由本类提供，ApiPostMixin 经 MRO 复用）。"""

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path

        if path in ("/", "/index.html"):
            _tick("WebView 来取页面了")
            p = wp("index.html")
            if not os.path.isfile(p):
                self._send(503, "页面还没生成", "text/plain; charset=utf-8")
                return
            with io.open(p, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        if path == "/api/ping":
            self._send(200, json.dumps({"app": APP_ID, "ver": APP_VERSION}, ensure_ascii=False))
            return

        if path == "/api/data":
            _tick("WebView 来取数据了")
            try:
                self._send(200, api_data_bytes())
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
            return

        if path.startswith("/img/"):
            core = urllib.parse.unquote(path[5:])
            raw, ctype = img_bytes(core)
            if not raw:
                self._send(404, b"", "text/plain; charset=utf-8")
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except Exception:
                pass
            return

        if path == "/api/focus":
            try:
                winref.MAIN_WIN.restore()
                winref.MAIN_WIN.show()
            except Exception:
                pass
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return

        if path == "/api/state":
            s = read_state()
            if not s.get("built"):
                try:
                    s["built"] = (get_payload().get("meta") or {}).get("built")
                except Exception:
                    pass
            # 告警只给管理号看（子账号 machines 不轮询，字段清空省事）
            if state.PERM_STATE.get("role") != "admin":
                s["alert_ts"] = 0
                s["alert_msg"] = ""
            self._send(200, json.dumps(s, ensure_ascii=False))
            return

        if path == "/api/updateall":
            # 全部更新（云端版，2026-10-09）：触发 GitHub Actions → 云端跑完自动同步
            # GET = 查进度（页面轮询用）；带 ok=true 的 GET?start=1 或 POST = 启动
            if u.query.startswith("start=1") or self.command == "POST":
                wf = "all"
                if self.command == "POST":
                    try:
                        n = int(self.headers.get("Content-Length") or 0)
                        b = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
                        wf = b.get("wf") if b.get("wf") in ("all", "kd", "yy") else "all"
                    except Exception:
                        pass
                if state._ALLRUN[0] or read_state()["status"] in ("running", "login"):
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
            self._send(200, json.dumps({"ok": True, "running": state._ALLRUN[0],
                                        "had_err": bool(state._ALLRUN[1]), "msg": state._ALLRUN[1],
                                        "summary": state._ALLRUN[2],
                                        "stage": state._ALLRUN[3], "pct": state._ALLRUN[4],
                                        "elapsed": int(time.time() - state._ALLRUN[5]) if state._ALLRUN[0] else 0},
                                       ensure_ascii=False))
            return

        if path == "/api/update":
            # 2026-10-10 全云端：本机直拉金山已退役 → 触发云端任务（同「全部更新」的 kd 分支）
            if read_state()["status"] in ("running", "login") or state._ALLRUN[0]:
                self._send(200, json.dumps({"ok": False, "running": state._ALLRUN[0],
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
            threading.Thread(target=_updateall_bg, args=(state.LOG.append, "kd"), daemon=True).start()
            self._send(200, json.dumps({"ok": True, "running": True}, ensure_ascii=False))
            return

        if path == "/api/stock":
            with state.STOCK_LOCK:
                self._send(200, json.dumps(state.STOCK, ensure_ascii=False))
            return

        if path == "/api/cancel":
            set_state("error", "已取消")
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return

        if path == "/api/appcheck":
            # 软件自更新：查云端（worker 转发 GitHub 最新 Release），跟本机版本比
            # 2026-10-11 Mac 端：worker 加了 ?plat=mac 匹配 ModelLookup-mac-*.zip 资产
            _ac = "/appcheck?plat=mac" if sys.platform == "darwin" else "/appcheck"
            j, err = cloud_req(_ac, timeout=30)
            if err or not j or not j.get("ok"):
                self._send(200, json.dumps({"ok": False,
                                            "msg": err or (j or {}).get("msg") or "查询失败"},
                                           ensure_ascii=False))
                return
            latest = str(j.get("ver") or "")
            self._send(200, json.dumps({
                "ok": True, "cur": APP_VERSION, "latest": latest, "tag": j.get("tag"),
                "name": j.get("name"), "size": j.get("size"),
                "newer": _ver_gt(latest, APP_VERSION)}, ensure_ascii=False))
            return

        if path == "/api/token":
            # 登录态镜像（读）：页面启动时 localStorage 没有 token 就从这里兜底恢复。
            # 背景：托盘「退出」会硬杀进程，WebView2 的 localStorage 偶发没落盘 → 每次都要重登。
            f = wp("auth_token.json")
            tok = None
            try:
                if os.path.isfile(f):
                    with open(f, encoding="utf-8") as fh:
                        tok = fh.read().strip() or None
            except Exception:
                tok = None
            self._send(200, json.dumps({"token": tok}, ensure_ascii=False))
            return

        self._send(404, json.dumps({"ok": False, "msg": "not found"}, ensure_ascii=False))
