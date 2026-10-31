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

        # ---------- 文案生成（2026-10-12，纯追加） ----------
        if path == "/api/copy":
            q = urllib.parse.parse_qs(u.query)
            model = (q.get("model") or [""])[0].strip()
            kind = (q.get("kind") or ["l"])[0]
            force = (q.get("force") or ["0"])[0] in ("1", "true")
            openfolder = (q.get("open") or ["0"])[0] in ("1", "true")
            style = (q.get("style") or [""])[0]   # v2=文案新口径（2026-10-21 显示开关）
            if not model:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model"}, ensure_ascii=False))
                return
            import copywriter
            if openfolder:
                # 打开图片文件夹（跨平台 reveal：Win 资源管理器 / Mac Finder）
                d = os.path.join(copywriter.copy_root(),
                                 copywriter.norm_model(model) or model)
                try:
                    from paths import reveal
                    reveal(d)
                    self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
                except Exception as e:
                    self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
                return
            bg = (q.get("bg") or ["0"])[0] in ("1", "true")
            if bg:
                # Mac WKWebView fetch ~60s 超时（"Load failed" 实锤）→ 文案生成走后台，前端轮询 /api/jobget
                import jobman

                def _do():
                    return copywriter.enrich(model, kind, force, style,
                                             log=lambda s: state.LOG.append("[文案] " + s))
                if not jobman.submit("copy:%s:%s" % (model, kind), _do):
                    self._send(200, json.dumps({"ok": False, "msg": "该型号文案已在生成中，等一下"},
                                               ensure_ascii=False))
                    return
                self._send(200, json.dumps({"ok": True, "started": True}, ensure_ascii=False))
                return
            try:
                res = copywriter.enrich(model, kind, force, style,
                                        log=lambda s: state.LOG.append("[文案] " + s))
                self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": "文案生成失败：%s" % e,
                                            "titles": [], "points": [], "desc": "", "xhs": [],
                                            "images": []}, ensure_ascii=False))
            return

        # ---------- 蓝底套图（2026-10-21，独立第三工具，与 /copy 写文案/婷宝配图零关系） ----------
        if path == "/api/kitset":
            q = urllib.parse.parse_qs(u.query)
            model = (q.get("model") or [""])[0].strip()
            force = (q.get("force") or ["0"])[0] in ("1", "true")
            openfolder = (q.get("open") or ["0"])[0] in ("1", "true")
            if not model:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model"}, ensure_ascii=False))
                return
            import kitset
            if openfolder:
                # 打开蓝底套图的图片文件夹（跨平台 reveal：Win 资源管理器 / Mac Finder）
                # 2026-10-31 重组后四张成片在 蓝底\ 子文件夹——打开要开到它，不能停型号根
                d = kitset.model_dir(model)
                bd = os.path.join(d, kitset.BLUE_SUB)
                try:
                    from paths import reveal
                    reveal(bd if os.path.isdir(bd) else d)
                    self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
                except Exception as e:
                    self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
                return
            if (q.get("bg") or ["0"])[0] in ("1", "true"):
                # Mac WKWebView fetch ~60s 超时（"Load failed" 实锤）→ 生成走后台，前端轮询 /api/jobget
                import jobman

                def _do():
                    try:
                        res = kitset.generate(model, force,
                                              log=lambda s: (state.LOG.append("[蓝底] " + s),
                                                             jobman.prog("kitset:" + model, s)))
                        res["dir"] = kitset.model_dir(model)
                        return res
                    except Exception as e:
                        # 2026-10-29 Mac 实锤：job 里炸了 app.log 啥都没有，远程没法查——
                        # 失败必记日志 + 上报诊断，再把异常抛回给 jobman 记进 job 状态
                        try:
                            state.LOG.append("[蓝底] 生成失败：%r" % e)
                            import cloud
                            threading.Thread(target=cloud.client_log_upload,
                                             args=("kitset:fail %s" % model,), daemon=True).start()
                        except Exception:
                            pass
                        raise
                if not jobman.submit("kitset:" + model, _do,
                                     _log=lambda s: state.LOG.append("[蓝底] " + s)):
                    self._send(200, json.dumps({"ok": False, "msg": "该型号已在生成中，等一下"},
                                               ensure_ascii=False))
                    return
                self._send(200, json.dumps({"ok": True, "started": True}, ensure_ascii=False))
                return
            try:
                import kitset
                res = kitset.generate(model, force,
                                      log=lambda s: state.LOG.append("[蓝底] " + s))
                res["dir"] = kitset.model_dir(model)
                self._send(200, json.dumps(res, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": "蓝底套图生成失败：%s" % e},
                                           ensure_ascii=False))
            return

        if path.startswith("/api/kitimg/"):
            # 蓝底套图产物（kitset/<型号>/蓝底/img_N.jpg / set.json / product.jpg），只许读 kitset 目录
            import kitset
            core = urllib.parse.unquote(path[len("/api/kitimg/"):])
            # 2026-10-31 目录重组：老数据（根目录 img_N / AI6张_聚光）先整一次再读
            _m = core.split("/", 1)[0]
            if _m:
                try:
                    kitset.model_dir(_m)
                except Exception:
                    pass
            root = os.path.abspath(kitset.kit_root())
            fp = os.path.abspath(os.path.join(kitset.kit_root(), core))
            if not fp.startswith(root) or not os.path.isfile(fp):
                self._send(404, json.dumps({"ok": False, "msg": "no such kit file"}, ensure_ascii=False))
                return
            ctype = "image/jpeg" if fp.lower().endswith((".jpg", ".jpeg")) else (
                "image/png" if fp.lower().endswith(".png") else "application/json")
            with open(fp, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/copyimg/"):
            # 文案配图（copy/<型号>/img_NN.jpg）：路径穿越防护，只许读 copy 目录
            import copywriter
            core = urllib.parse.unquote(path[len("/copyimg/"):])
            parts = core.split("/")
            if (len(parts) != 2 or not all(parts)
                    or ".." in parts[0] or ".." in parts[1] or "/" in parts[0] or "\\" in parts[0]
                    or "/" in parts[1] or "\\" in parts[1]):
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            fp = os.path.join(copywriter.copy_root(), parts[0], parts[1])
            root = os.path.abspath(copywriter.copy_root())
            if not os.path.abspath(fp).startswith(root) or not os.path.isfile(fp):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            ext = os.path.splitext(fp)[1].lower()
            ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".webp": "image/webp"}.get(ext, "application/octet-stream")
            with io.open(fp, "rb") as f:
                raw = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except Exception:
                pass
            return
        # ---------- 文案路由结束 ----------

        if path in ("/", "/index.html"):
            _tick("WebView 来取页面了")
            p = wp("index.html")
            if not os.path.isfile(p):
                self._send(503, "页面还没生成", "text/plain; charset=utf-8")
                return
            with io.open(p, "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return

        if path == "/api/xycookie":
            # 闲鱼直连配套（2026-10-14）：这个号存没存 Cookie——只回有没有，绝不回内容
            import xyops
            q = urllib.parse.parse_qs(u.query)
            acc = (q.get("acc") or [""])[0].strip()[:24]
            self._send(200, json.dumps({"ok": True, "has": xyops.has_cookie(acc)},
                                       ensure_ascii=False))
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
            # 闲鱼页自动化（CDP）开关状态（2026-10-24）：设置面板显示用，本机文件为准
            try:
                s["cdp_on"] = not os.path.isfile(wp("xy_cdp_off.txt"))
            except Exception:
                s["cdp_on"] = True
            # 开机自启状态（2026-10-25）：设置面板显示用（Mac 没有原生菜单，页面是唯一入口）
            try:
                from appwin import autostart_state
                s["autostart_on"] = bool(autostart_state())
            except Exception:
                s["autostart_on"] = True
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
                if state.PERM_STATE.get("role") not in (None, "admin"):
                    self._send(200, json.dumps({"ok": False, "msg": "只有管理号能触发云端更新（子账号的数据会自动跟着云端走）"},
                                               ensure_ascii=False))
                    return
                if not state.allrun_try_start(lambda: read_state()["status"] in ("running", "login")):
                    self._send(200, json.dumps({"ok": False, "running": True,
                                                "msg": "已有更新在进行中"}, ensure_ascii=False))
                    return
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
            if state.PERM_STATE.get("role") not in (None, "admin"):
                self._send(200, json.dumps({"ok": False, "msg": "只有管理号能触发云端更新（子账号的数据会自动跟着云端走）"},
                                           ensure_ascii=False))
                return
            if not state.allrun_try_start(lambda: read_state()["status"] in ("running", "login")):
                self._send(200, json.dumps({"ok": False, "running": state._ALLRUN[0],
                                            "msg": "已有更新在进行中"}, ensure_ascii=False))
                return
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

        # ---------- 发布图工厂（2026-10-17，闲鱼配文 Pro 并入） ----------
        if path == "/api/pubwork":
            # 抓素材：本地已有 info.json 直接返回；没有则起后台线程走抓取链，前端轮询 /api/pubjob
            q = urllib.parse.parse_qs(u.query)
            model = (q.get("model") or [""])[0].strip()
            if not model:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model"}, ensure_ascii=False))
                return
            import pubwork
            info = pubwork.load_info(model)
            if info and not (q.get("force") or ["0"])[0] in ("1", "true"):
                self._send(200, json.dumps({"ok": True, "cached": True, "info": {
                    "brand": info.get("brand"), "model": info.get("model"),
                    "title": info.get("title") or info.get("pname"), "price": info.get("price"),
                    "props": info.get("props") or [], "n_imgs": len(pubwork._img_files(info["dir"]))},
                    "job": pubwork.job_state()}, ensure_ascii=False))
                return
            if pubwork.job_state()["running"]:
                self._send(200, json.dumps({"ok": False, "msg": "已有任务在跑：" + pubwork.busy_msg()}, ensure_ascii=False))
                return
            pubwork._job_begin(model, "抓素材")
            def _w():
                try:
                    inf = pubwork.work(model, prog=pubwork._job_prog)
                    pubwork._save_info(inf)
                    pubwork._job_prog(100, "素材就绪")
                    pubwork._job_end(True)
                except Exception as e:
                    pubwork._job_end(False, str(e))
            threading.Thread(target=_w, daemon=True).start()
            self._send(200, json.dumps({"ok": True, "started": True}, ensure_ascii=False))
            return

        if path == "/api/publib":
            q = urllib.parse.parse_qs(u.query)
            model = (q.get("model") or [""])[0].strip()
            if not model:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model"}, ensure_ascii=False))
                return
            import pubwork
            self._send(200, json.dumps(pubwork.lib(model), ensure_ascii=False))
            return

        if path == "/api/pubai":
            # AI 连接状态：ping=1 时真调一次 GLM（对齐闲鱼配文pro「AI 连接测试」按钮）
            import copywriter
            q = urllib.parse.parse_qs(u.query)
            if (q.get("ping") or ["0"])[0] in ("1", "true"):
                try:
                    r = copywriter._chat("只输出两个字符：OK")
                    ok = bool(r) and "OK" in str(r).upper()
                    self._send(200, json.dumps({"ok": True, "ai": bool(copywriter._load_glm_key()),
                                                "ping": "ok" if ok else "fail",
                                                "raw": str(r)[:60]}, ensure_ascii=False))
                except Exception as e:
                    self._send(200, json.dumps({"ok": True, "ai": bool(copywriter._load_glm_key()),
                                                "ping": "fail", "msg": str(e)[:120]}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"ok": True, "ai": bool(copywriter._load_glm_key())},
                                       ensure_ascii=False))
            return

        if path == "/api/pubjob":
            import pubwork
            self._send(200, json.dumps(pubwork.job_state(), ensure_ascii=False))
            return

        # ---------- 增强层 GET（2026-10-31 用户令「剩下的全改」） ----------
        if path == "/api/ex" or path.startswith("/api/ex/"):
            import extras
            q = urllib.parse.parse_qs(u.query)
            sub = path[len("/api/ex"):].strip("/")
            try:
                if sub == "":
                    r = extras.get_all()
                elif sub == "chg":
                    r = {"ok": True, "list": extras._load()["chg"][:600]}
                elif sub == "wall":
                    r = extras.wall()
                elif sub == "params":
                    r = extras.params_of((q.get("model") or [""])[0].strip())
                elif sub == "precheck":
                    r = extras.precheck((q.get("model") or [""])[0].strip(),
                                        (q.get("price") or [None])[0])
                elif sub == "jobs":
                    r = extras.jobs()
                elif sub == "changelog":
                    r = extras.changelog()
                elif sub == "hist":
                    r = extras.hist_of((q.get("model") or [""])[0].strip())
                elif sub == "copylist":
                    r = extras.list_copy((q.get("model") or [""])[0].strip())
                elif sub == "quota":
                    r = extras.quota_info()
                elif sub == "cands":
                    r = extras.candidates((q.get("model") or [""])[0].strip())
                else:
                    r = {"ok": False, "msg": "unknown"}
            except Exception as e:
                r = {"ok": False, "msg": extras.rep(e)}
            self._send(200, json.dumps(r, ensure_ascii=False))
            return

        if path == "/api/kitone":
            # 只重出单张（2026-10-31「只重出单张」）：n=1..4，走后台 + jobman 轮询
            q = urllib.parse.parse_qs(u.query)
            model = (q.get("model") or [""])[0].strip()
            try:
                n = int((q.get("n") or ["1"])[0])
            except Exception:
                n = 1
            if not model:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model"}, ensure_ascii=False))
                return
            import jobman
            import kitset
            key = "kitone:%s:%d" % (model, n)

            def _do():
                return kitset.regen_one(model, n, log=lambda s: (
                    state.LOG.append("[蓝底·单张] " + s), jobman.prog(key, s)))
            if not jobman.submit(key, _do, _log=lambda s: state.LOG.append("[蓝底·单张] " + s)):
                self._send(200, json.dumps({"ok": False, "msg": "这张正在重出中，等一下"},
                                           ensure_ascii=False))
                return
            self._send(200, json.dumps({"ok": True, "started": True, "key": key}, ensure_ascii=False))
            return

        if path == "/api/jobget":
            # 通用后台任务状态（2026-10-25，配合 jobman）：?key=kitset:<型号> / copy:<型号>:<kind> / pubqa:<型号>
            q = urllib.parse.parse_qs(u.query)
            jk = (q.get("key") or [""])[0].strip()
            if not jk:
                self._send(400, json.dumps({"ok": False, "msg": "缺 key"}, ensure_ascii=False))
                return
            import jobman
            j = jobman.state(jk)
            if j.get("running"):
                j.update(ok=True, phase="running")
            elif j.get("done"):
                if j.get("ok"):
                    j["phase"] = "done"
                else:
                    j["phase"] = "failed"
                    j["res"] = {"ok": False, "msg": j.get("err") or "任务失败"}
            else:
                j.update(ok=True, phase="idle")
            self._send(200, json.dumps(j, ensure_ascii=False))
            return

        if path == "/api/xymenu":
            # 2026-10-17：运行时菜单路由探测（CDP 在已登录 goofish 页里调 mtop 菜单接口），
            # 用来把「改价/下架」指到真实的宝贝管理页；raw=菜单原始 JSON，routes_seen=嗅探到的 hash
            try:
                import xy_cdp
                raw = xy_cdp.dump_menu()
                self._send(200, json.dumps(
                    {"ok": True, "raw": raw or "(没探到：先在软件里打开一次「数据」并登录)",
                     "routes_seen": xy_cdp.routes_seen()}, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "msg": str(e)[:200]}, ensure_ascii=False))
            return

        if path.startswith("/pubimg/"):
            # 发布图工厂静态图：/pubimg/<型号>/<文件> 或 /pubimg/<型号>/<子目录>/<文件>
            import pubwork
            core = urllib.parse.unquote(path[len("/pubimg/"):])
            parts = core.split("/")
            if not (2 <= len(parts) <= 3) or not all(parts) or any(
                    (".." in p) or ("/" in p) or ("\\" in p) for p in parts):
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            if len(parts) >= 2 and parts[1] in (pubwork.SPOT_SUB, "AI6张_聚光"):
                # 聚光四件套成品在 kitset/<型号>/聚光（2026-10-31 改名，旧请求名自动映射）
                if parts[1] != pubwork.SPOT_SUB:
                    parts[1] = pubwork.SPOT_SUB
                import kitset
                base = kitset.model_dir(parts[0])
                parts = parts[1:]
            else:
                base = pubwork.sroot()
            fp = os.path.join(base, *parts)
            root = os.path.abspath(base)
            if not os.path.abspath(fp).startswith(root) or not os.path.isfile(fp):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            ext = os.path.splitext(fp)[1].lower()
            ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".webp": "image/webp", ".bmp": "image/bmp"}.get(ext, "application/octet-stream")
            with io.open(fp, "rb") as f:
                raw = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except Exception:
                pass
            return

        self._send(404, json.dumps({"ok": False, "msg": "not found"}, ensure_ascii=False))
