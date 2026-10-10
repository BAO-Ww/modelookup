# -*- coding: utf-8 -*-
"""本地 HTTP 服务（2026-10-11 拆分一期：从 main.py 原样搬出，纯搬运零行为变化）。
Handler 用 mixin 组装（Python 标准惯用法，self 共享无需改逻辑）；
MLServer/start_server 在这里（单实例互斥/端口登记在 instance.py）。
依赖方向：server 是依赖图顶层（Handler 触碰 7 个域）。"""
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
