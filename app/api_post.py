# -*- coding: utf-8 -*-
"""本地服务 POST 路由（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
ApiPostMixin.do_POST——_send 经 MRO 复用 ApiGetMixin 的。
依赖方向：server 顶层（这里触到 appwin.rebuild_menu_for，方向 server→appwin 合法）。"""
import base64
import json
import os
import sys
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
                # 2026-10-09 用户要求：标题栏显示当前登录账号（2026-10-13：去掉「（子账号）/（管理号）」后缀）
                try:
                    _u = str(d.get("u") or "")
                    if winref.MAIN_WIN is not None:
                        winref.MAIN_WIN.set_title("型号查询 — %s" % _u if _u else "型号查询")
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
            # 2026-10-11 Mac 端：更新链按平台分发（Windows 链不动，Mac 走 updater_mac）
            if sys.platform == "darwin":
                from updater_mac import _app_install_bg as _app_bg
            else:
                _app_bg = _app_install_bg
            threading.Thread(target=_app_bg, args=(state.LOG.append,), daemon=True).start()
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
        if u.path in ("/api/copypolish", "/api/copyqa", "/api/copypick"):
            # 文案生成增强（2026-10-13 对齐闲鱼配文pro，纯追加）：
            #   copypolish  AI 润色（target=titles|desc）
            #   copyqa      AI 自检（标题+卖点+描述整体挑毛病）
            #   copypick    图片勾选发布顺序 → 按顺序复制进 copy/<型号>/精选/
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            model = str(d.get("model") or "").strip()
            kind = 's' if d.get("kind") == 's' else 'l'
            if not model:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model"}, ensure_ascii=False))
                return
            import copywriter
            logf = lambda s: state.LOG.append("[文案] " + s)
            try:
                if u.path == "/api/copypolish":
                    tgt = 'titles' if d.get("target") == 'titles' else 'desc'
                    self._send(200, json.dumps(
                        copywriter.polish(model, kind, tgt, d.get("text") or "", logf),
                        ensure_ascii=False))
                    return
                if u.path == "/api/copyqa":
                    self._send(200, json.dumps(
                        copywriter.qa_copy(model, kind, d.get("text") or "", logf),
                        ensure_ascii=False))
                    return
                # copypick：勾选发布顺序落精选文件夹
                files = [str(x) for x in (d.get("files") or [])
                         if str(x) and ".." not in str(x) and "/" not in str(x) and "\\" not in str(x)]
                if not files:
                    self._send(400, json.dumps({"ok": False, "msg": "没勾选图片"}, ensure_ascii=False))
                    return
                root = copywriter.copy_root()
                src_dir = os.path.join(root, copywriter.norm_model(model) or model)
                dst = os.path.join(src_dir, "精选")
                os.makedirs(dst, exist_ok=True)
                cnt = 0
                for i, fn in enumerate(files):
                    sp = os.path.join(src_dir, fn)
                    if not (os.path.isfile(sp) and os.path.abspath(sp).startswith(os.path.abspath(root))):
                        continue
                    ext = os.path.splitext(fn)[1].lower() or ".jpg"
                    import shutil
                    shutil.copyfile(sp, os.path.join(dst, "%02d%s" % (i + 1, ext)))
                    cnt += 1
                try:
                    os.startfile(dst)  # noqa 仅 Windows
                except Exception:
                    pass
                self._send(200, json.dumps({"ok": True, "count": cnt, "dir": "精选"},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
            return

        if u.path in ("/api/xycookie", "/api/xyprice", "/api/xyoff"):
            # 闲鱼直连（2026-10-14 用户需求：软件里直接改价/下架，不打开网页）。
            # Cookie 只存本机 wp("xy_cookies.json")（不进云端，粘一次用一阵子）；
            # mtop 调用阻塞几秒——ThreadingHTTPServer 每请求各线程，不卡别的页面请求。
            import xyops
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            acc = str(d.get("acc") or "").strip()[:24]
            if u.path == "/api/xycookie":
                if not acc:
                    self._send(400, json.dumps({"ok": False, "msg": "缺闲鱼号名"}, ensure_ascii=False))
                    return
                r = xyops.save_cookie(acc, d.get("cookie") or "")
            elif u.path == "/api/xyprice":
                r = xyops.set_price(acc, d.get("id"), d.get("p"))
            else:
                r = xyops.off_shelf(acc, d.get("id"))
            state.LOG.append("[闲鱼] %s id=%s acc=%s → %s" % (
                u.path, d.get("id"), acc, (r or {}).get("msg") or (r or {}).get("ok")))
            self._send(200, json.dumps(r, ensure_ascii=False))
            return

        if u.path == "/api/cdp_switch":
            # 闲鱼页自动化（CDP 调试口）开关（2026-10-24）：本机 wp("xy_cdp_off.txt")，
            # 存在=关。改的是下次启动的生效状态（WebView2 调试口启动时定死），重启软件生效。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            on = bool(d.get("on"))
            p = wp("xy_cdp_off.txt")
            try:
                if on:
                    try: os.remove(p)
                    except FileNotFoundError: pass
                else:
                    with open(p, "w", encoding="utf-8") as f:
                        f.write("off")
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
                return
            state.LOG.append("[设置] 闲鱼页自动化（CDP）→ %s（重启软件生效）" % ("开" if on else "关"))
            self._send(200, json.dumps({"ok": True, "on": on, "need_restart": True}, ensure_ascii=False))
            return

        if u.path == "/api/xyview":
            # 软件内打开闲鱼页（2026-10-17）：拉起 --goofish 独立进程窗口，
            # 一号一个 WebView2 档案（xyprofile_<账号>），多号登录态互不干扰
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            url = str(d.get("url") or "")
            acc = str(d.get("acc") or "default")[:24]
            if not url.startswith(("https://www.goofish.com/", "https://seller.goofish.com/",
                                   "https://www.goofish.com")):
                self._send(400, json.dumps({"ok": False, "msg": "只许打开 goofish.com 页面"},
                                           ensure_ascii=False))
                return
            import subprocess
            if getattr(sys, "frozen", False):
                cmd = [sys.executable, "--goofish", acc, url]
            else:
                cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                    "main.py"), "--goofish", acc, url]
            flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
            try:
                subprocess.Popen(cmd, creationflags=flags, close_fds=True)
                state.LOG.append("[闲鱼] 内嵌窗口：%s（%s）" % (url[:60], acc))
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
            return

        if u.path == "/api/xyopen":
            # 2026-10-17：系统浏览器打开 goofish 页（管理号把子账号的
            # 「闲鱼操作在软件内打开」勾掉时，编辑/改价/查看/下架走这条路）
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            url2 = str(d.get("url") or "")
            if not url2.startswith(("https://www.goofish.com/", "https://seller.goofish.com/")):
                self._send(400, json.dumps({"ok": False, "msg": "只许打开 goofish.com 页面"},
                                           ensure_ascii=False))
                return
            try:
                import webbrowser
                webbrowser.open(url2)
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
            return

        # ---------- 素材存储路径（2026-10-24 用户定稿：界面可改，peiwork+copy 共用，老数据搬） ----------
        if u.path in ("/api/pubroot", "/api/pubrootset"):
            import pubwork
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            if u.path == "/api/pubroot":
                r = pubwork.root_status()
            else:
                if pubwork.job_state()["running"]:
                    r = {"ok": False, "msg": "有素材任务在跑，等它跑完再改"}
                else:
                    r = pubwork.root_move(str(d.get("path") or ""))
            self._send(200, json.dumps(r, ensure_ascii=False))
            return

        # ---------- 发布图工厂（2026-10-17，闲鱼配文 Pro 并入） ----------
        if u.path in ("/api/pubgen", "/api/pubhero", "/api/pubsel", "/api/pubqa",
                      "/api/pubauto", "/api/pubimport", "/api/pubgh", "/api/pubopen",
                      "/api/pubxy"):
            import pubwork
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            model = str(d.get("model") or "").strip()
            if not model:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model"}, ensure_ascii=False))
                return
            price = d.get("price")
            try:
                price = float(price) if price not in (None, "", 0) else None
            except (TypeError, ValueError):
                price = None
            ver = "B" if d.get("ver") == "B" else "N"
            try:
                if u.path == "/api/pubgen":
                    # 套图生成走后台线程（AI 背景要十几秒），前端轮询 /api/pubjob
                    if pubwork.job_state()["running"]:
                        self._send(200, json.dumps({"ok": False, "msg": "已有任务在跑"}, ensure_ascii=False))
                        return
                    style = str(d.get("style") or "studio")
                    if style not in pubwork.STYLE_DIRS:
                        self._send(400, json.dumps({"ok": False, "msg": "未知风格"}, ensure_ascii=False))
                        return
                    threading.Thread(target=pubwork.gen_style,
                                     args=(model, style, price, ver), daemon=True).start()
                    self._send(200, json.dumps({"ok": True, "started": True}, ensure_ascii=False))
                    return
                if u.path == "/api/pubhero":
                    if pubwork.job_state()["running"]:
                        self._send(200, json.dumps({"ok": False, "msg": "已有任务在跑"}, ensure_ascii=False))
                        return
                    threading.Thread(target=pubwork.gen_hero,
                                     args=(model, price, ver), daemon=True).start()
                    self._send(200, json.dumps({"ok": True, "started": True}, ensure_ascii=False))
                    return
                if u.path == "/api/pubsel":
                    rels = [str(x).replace("\\", "/") for x in (d.get("files") or [])
                            if str(x) and ".." not in str(x)]
                    r = pubwork.sel_save(model, rels, sell_price=price, version=ver)
                elif u.path == "/api/pubqa":
                    r = pubwork.qa_run(model)
                elif u.path == "/api/pubauto":
                    r = pubwork.qa_auto(model)
                elif u.path == "/api/pubimport":
                    r = pubwork.import_imgs(model, d.get("files") or [])
                elif u.path == "/api/pubgh":
                    r = pubwork.push_gh(model, only_sel=bool(d.get("sel")))
                elif u.path == "/api/pubxy":
                    # 一键发布闲鱼：playwright 半自动，阻塞在后台线程（用户在浏览器点发布）
                    if pubwork.job_state()["running"]:
                        self._send(200, json.dumps({"ok": False, "msg": "已有任务在跑"}, ensure_ascii=False))
                        return
                    threading.Thread(target=pubwork.pub_xy,
                                     args=(model, ver), daemon=True).start()
                    self._send(200, json.dumps({"ok": True, "started": True,
                                                "msg": "浏览器即将打开发布页，自动填好后你自己点「发布」"},
                                               ensure_ascii=False))
                    return
                else:
                    r = pubwork.open_dir(model, str(d.get("sub") or ""))
                state.LOG.append("[发布图] %s %s" % (u.path, model))
                self._send(200, json.dumps(r, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
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
