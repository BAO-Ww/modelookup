# -*- coding: utf-8 -*-
"""本地 HTTP 服务（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
Handler 用 mixin 组装（Python 标准惯用法，self 共享无需改逻辑）；
MLServer/start_server 在这里（单实例互斥/端口登记在 instance.py）。
依赖方向：server 是依赖图顶层（Handler 触碰 7 个域）。"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from api_get import ApiGetMixin
from api_post import ApiPostMixin
from config import HOST, PORTS
from instance import save_port


class Handler(ApiGetMixin, ApiPostMixin, BaseHTTPRequestHandler):
    server_version = "ModelLookup/2.7.2"

    def log_message(self, *a):
        pass

    def _guarded(self, meth):
        """接口兜底（2026-10-29 Mac 实锤）：handler 里任何未捕获异常（如缺模块的
        ImportError）以前会直接掐连接——Mac WKWebView 报 "Load failed"、零痕迹没法查。
        现在记进 app.log + 上报诊断，再回 500 JSON 让前端把真实错误显示出来。"""
        try:
            meth(self)
        except Exception as e:
            try:
                import state
                state.LOG.append("[服务] 接口异常 %s: %r" % (self.path[:80], e))
            except Exception:
                pass
            try:
                import cloud
                threading.Thread(target=cloud.client_log_upload,
                                 args=("api:crash " + str(e)[:60],), daemon=True).start()
            except Exception:
                pass
            try:
                self._send(500, json.dumps({"ok": False, "msg": "服务接口异常：%r" % (e,)},
                                           ensure_ascii=False))
            except Exception:
                pass

    def do_GET(self):
        self._guarded(ApiGetMixin.do_GET)

    def do_POST(self):
        self._guarded(ApiPostMixin.do_POST)


class MLServer(ThreadingHTTPServer):
    # Windows 下 HTTPServer 默认开 SO_REUSEADDR：新实例能跟一个僵死实例**同时绑上
    # 同一个端口**，连接却流进僵尸的尸体 socket → 软件看起来起了、页面永远打不开
    # （2026-10-10 僵尸 4804 实锤）。关掉它：端口被占就干净地落到下一个。
    allow_reuse_address = 0


def start_server():
    httpd = None
    port = 0
    for p in PORTS:
        try:
            httpd = MLServer((HOST, p), Handler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:                       # 全被别的东西占了才退回随机端口
        s = socket.socket()
        s.bind((HOST, 0))
        port = s.getsockname()[1]
        s.close()
        httpd = MLServer((HOST, port), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    save_port(port)
    return httpd, port
