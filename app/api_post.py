# -*- coding: utf-8 -*-
"""本地服务 POST 路由（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
ApiPostMixin.do_POST——_send 经 MRO 复用 ApiGetMixin 的。
依赖方向：server 顶层（这里触到 appwin.rebuild_menu_for，方向 server→appwin 合法）。"""
import base64
import json
import os
import shutil
import sys
import threading
import urllib.parse

import state
import winref
from appwin import rebuild_menu_for
from cloud import stock_pull_cloud
from paths import wp
from state import read_state, set_state
from updater import _app_install_bg, _suball_bg, _updateall_bg, do_update


def _mk_refresh_bg():
    """登录/换号后刷新拿货价加价值（2026-11-26）：worker /mk 拉最新，失败静默。"""
    try:
        import cloud as _cl
        _cl.markup_get(refresh=True)
    except Exception:
        pass


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
                # 2026-11-02 用户令：标题要带版本号——型号查询 v3.0.34 — B（版本号从 config 取，别再写死）
                try:
                    _u = str(d.get("u") or "")
                    if winref.MAIN_WIN is not None:
                        try:
                            from config import APP_TITLE as _TT
                        except ImportError:
                            _TT = "型号查询"
                        winref.MAIN_WIN.set_title(("%s — %s" % (_TT, _u)) if _u else _TT)
                except Exception:
                    pass
                # 按角色重画原生菜单：子账号只留 账号管理/退出登录
                threading.Thread(target=rebuild_menu_for,
                                 args=(d.get("role"),), daemon=True).start()
                # 2026-11-26 拿货价加价：登录/换号立即刷新本机加价值（平时 10 分钟缓存）
                # worker 管理号改了 mk → 子用户这台机下次登录或最迟 10 分钟跟进
                threading.Thread(target=_mk_refresh_bg, daemon=True).start()
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
        if u.path == "/api/pubcache":
            # /puball 整包的落盘镜像（写）：整包 ~27KB，整文件覆盖写，坏了也不会撑爆盘。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n and n < 2 * 1024 * 1024:
                    raw = self.rfile.read(n)
                    json.loads(raw.decode("utf-8"))        # 先验 JSON，坏的绝不落盘
                    with open(wp("pubcache.json"), "wb") as fh:
                        fh.write(raw)
            except Exception as e:
                state.LOG.append("发布缓存镜像失败：%s" % e)
            self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
            return
        if u.path == "/api/stock":
            # 2026-10-10 全云端：管理号→触发云端拉用友（同「全部更新」的 yy 分支）；
            # 子账号→直接从云端同步。本机 CAS 直拉/自动登录已退役（错密码撞锁号风险）。
            if state.PERM_STATE.get("role") not in (None, "admin"):
                set_state("running", "正在从云端同步库存…")
                threading.Thread(target=stock_pull_cloud, args=(state.LOG.append,),
                                 daemon=True).start()
                self._send(200, json.dumps({"ok": True}, ensure_ascii=False))
                return
            if not state.allrun_try_start(lambda: read_state()["status"] in ("running", "login")):
                self._send(200, json.dumps({"ok": False, "running": state._ALLRUN[0],
                                            "msg": "已有更新在进行中"}, ensure_ascii=False))
                return
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
            if state.PERM_STATE.get("role") not in (None, "admin"):
                # 2026-11-05 用户令：子账号的「全部更新」只从云端拉现成数据，
                # 不触发云端一条龙（citrigger 是管理号专属）
                if not state.allrun_try_start(lambda: read_state()["status"] in ("running", "login")):
                    self._send(200, json.dumps({"ok": False, "running": True,
                                                "msg": "已有更新在进行中"}, ensure_ascii=False))
                    return
                set_state("running", "正在从云端同步数据…")
                threading.Thread(target=_suball_bg, args=(state.LOG.append, wf), daemon=True).start()
                self._send(200, json.dumps({"ok": True, "running": True}, ensure_ascii=False))
                return
            if not state.allrun_try_start(lambda: read_state()["status"] in ("running", "login")):
                self._send(200, json.dumps({"ok": False, "running": True,
                                            "msg": "已有更新在进行中"}, ensure_ascii=False))
                return
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
                    _mode = 'rewrite' if d.get("mode") == 'rewrite' else 'polish'
                    self._send(200, json.dumps(
                        copywriter.polish(model, kind, tgt, d.get("text") or "", logf, mode=_mode),
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
                    shutil.copyfile(sp, os.path.join(dst, "%02d%s" % (i + 1, ext)))
                    cnt += 1
                try:
                    from paths import reveal
                    reveal(dst)
                except Exception:
                    pass
                self._send(200, json.dumps({"ok": True, "count": cnt, "dir": "精选"},
                                           ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
            return

        if u.path == "/api/autostart":
            # 开机自启开关（2026-10-25）：页面「软件设置（本机）」用——Mac 没有原生菜单，
            # 页面是双平台统一入口。立即生效（写/删配置 + autostart_off.txt 标记）。
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            on = bool(d.get("on"))
            try:
                from appwin import autostart_turn
                ok = bool(autostart_turn(on, state.LOG.append))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
                return
            state.LOG.append("[设置] 开机自启 → %s" % ("开" if on else "关"))
            self._send(200, json.dumps({"ok": ok, "on": on}, ensure_ascii=False))
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
            # 2026-10-26 修：改 hostname 精确比对。原 startswith 含无尾斜杠项
            # "https://www.goofish.com"，https://www.goofish.com.evil.com/ 可绕过
            try:
                from urllib.parse import urlparse as _urlparse
                _pu = _urlparse(url)
                _host_ok = _pu.scheme == "https" and \
                    (_pu.hostname or "").lower() in ("www.goofish.com", "seller.goofish.com")
            except Exception:
                _host_ok = False
            if not _host_ok:
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

        # ---------- 增强层（2026-10-31 用户令「剩下的全改」）：一个入口全兜住 ----------
        # 别名/标签/改价历史退回/文案多版本/链接回填/水印/快照比对/嫌疑价/配额/备份/诊断包
        if u.path in ("/api/ex", "/api/exbak", "/api/exdiag"):
            import extras
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}") if n else {}
            except Exception:
                d = {}
            act = str(d.get("act") or "")
            try:
                if u.path == "/api/exbak":
                    r = extras.backup()
                elif u.path == "/api/exdiag":
                    r = extras.diag()
                elif act == "alias":
                    r = extras.set_alias(d.get("m"), d.get("names"))
                elif act == "tag":
                    r = extras.add_tag(d.get("m"), d.get("tag"))
                elif act == "untag":
                    r = extras.del_tag(d.get("m"), d.get("tag"))
                elif act == "hist":
                    r = extras.add_hist(d.get("m"), d.get("xy"), d.get("o"), d.get("n"), d.get("by"))
                elif act == "undo":
                    r = extras.undo_hist(d.get("m"), d.get("xy") or "")
                elif act == "copy":
                    r = extras.save_copy(d.get("m"), d.get("title"), d.get("body"),
                                         d.get("kind") or "z", d.get("tag") or "")
                elif act == "copydel":
                    r = extras.del_copy(d.get("m"), d.get("t"))
                elif act == "link":
                    r = extras.set_link(d.get("m"), d.get("xid"), d.get("url"),
                                        d.get("price"), d.get("xy"))
                elif act == "natforce":
                    # 2026-11-21 管理号令：发往不读表定死「全国 · 100运费」（开关）
                    r = extras.set_natforce(d.get("m"), bool(d.get("on")))
                elif act == "water":
                    r = extras.set_water(**{k: d[k] for k in ("on", "text", "pos", "size",
                                                              "color", "alpha") if k in d})
                elif act == "snap":
                    # 一次报两路：prices / stocks，前端按返回的 changed 亮条
                    r = {"ok": True, "price": extras.snap("p", d.get("prices") or {}),
                         "stock": extras.snap("s", d.get("stocks") or {})}
                elif act == "setmain":
                    r = extras.set_main(d.get("m"), d.get("rel"))
                elif act == "suspects":
                    r = extras.suspects(d.get("rows") or [])
                elif act == "log":
                    extras.log(d.get("k") or "misc", d.get("m") or "", d.get("x") or "")
                    r = {"ok": True}
                elif act == "quota":
                    q = extras.quota_bump(int(d.get("n") or 1))
                    r = {"ok": True, "quota": q}
                elif act == "citrigger":
                    # 云端体检一键重派（2026-11-01）：只触发不等待（跑完各机器自动同步）。
                    # 权限由 worker /citrigger 上游把门（管理号或勾了更新面板权限）。
                    import cloud as _cl
                    try:
                        j, err = _cl.cloud_req("/citrigger", "POST",
                                               {"wf": str(d.get("wf") or "all")}, timeout=60)
                        r = j if (j and j.get("ok") and not err) else \
                            {"ok": False, "msg": err or (j or {}).get("msg") or "触发失败"}
                    except Exception as e:
                        r = {"ok": False, "msg": str(e)}
                else:
                    r = {"ok": False, "msg": "未知操作：" + act}
            except Exception as e:
                r = {"ok": False, "msg": extras.rep(e)}
            self._send(200, json.dumps(r, ensure_ascii=False))
            return

        # ---------- 选图面板手选落盘（2026-11-26 用户令「全部铺出来我来选主图跟尺寸图」） ----------
        # 点选候选图 → 复制为 官网/主图_手选.<ext> / 官网/尺寸图_手选.<ext>（清掉同槽
        # 旧变体保证 _find 命中唯一）→ _manual_overrides 立即应用落 product.png /
        # sizeimg.jpg。skipsize=1 = 用户明确「无尺寸图出图」，只记态不落盘。
        if u.path == "/api/kitpick":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            model = str(d.get("model") or "").strip()
            if not model:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model"}, ensure_ascii=False))
                return
            import kitset as _kp
            try:
                mdir = _kp.model_dir(model)
            except Exception as e:
                self._send(400, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
                return
            if str(d.get("skipsize") or "") in ("1", "true"):
                self._send(200, json.dumps({"ok": True, "skipsize": True}, ensure_ascii=False))
                return
            kind = str(d.get("kind") or "")
            rel = str(d.get("p") or "").replace("\\", "/").strip()
            src = os.path.abspath(os.path.join(mdir, rel))
            if not src.startswith(os.path.abspath(mdir) + os.sep) or not os.path.isfile(src):
                self._send(400, json.dumps({"ok": False, "msg": "候选路径不合法或文件不存在"},
                                           ensure_ascii=False))
                return
            od = os.path.join(mdir, "官网")
            os.makedirs(od, exist_ok=True)
            _ext = os.path.splitext(src)[1].lower() or ".jpg"
            # 2026-11-27 用户令「尺寸图未必只有一台，像 KF70/200 就有两张，选两个合并」：
            # kind=size2 落 尺寸图2_手选，出图时 _manual_overrides 纵向合并成一张 sizeimg。
            _kind_stem = {"main": "主图_手选", "size": "尺寸图_手选",
                          "size2": "尺寸图2_手选"}.get(kind)
            if not _kind_stem:
                self._send(400, json.dumps({"ok": False, "msg": "kind 不合法"},
                                           ensure_ascii=False))
                return
            # 清掉本槽全部旧变体（上传的 主图.png 和历史手选 主图_*.png 一起清，防 _find 挑错）
            # 注意：清 尺寸图 槽不许波及 尺寸图2（那是另一台机器的图）
            for f in os.listdir(od):
                _st, _ex = os.path.splitext(f)
                if _ex.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                    continue
                _hit = False
                if kind == "main":
                    _hit = (_st == "主图" or _st.startswith("主图"))
                elif kind == "size":
                    _hit = ((_st.startswith(("尺寸图", "尺寸")) and
                             not _st.startswith(("尺寸图2", "尺寸2"))) or _st == "zol_0")
                elif kind == "size2":
                    _hit = _st.startswith(("尺寸图2", "尺寸2"))
                if _hit:
                    try:
                        os.remove(os.path.join(od, f))
                    except Exception:
                        pass
            dst = os.path.join(od, _kind_stem + _ext)
            try:
                shutil.copyfile(src, dst)
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": "落盘失败：%r" % e},
                                           ensure_ascii=False))
                return
            _kp._manual_overrides(mdir, lambda s: None)
            okm = os.path.isfile(os.path.join(mdir, "product.png"))
            oks = os.path.isfile(os.path.join(mdir, "sizeimg.jpg"))
            self._send(200, json.dumps({"ok": True, "kind": kind, "main": okm, "size": oks,
                                        "ready": (okm and (oks or kind == "main"))},
                                       ensure_ascii=False))
            return

        # ---------- 手动塞主图/尺寸图（2026-11-23 用户令「四层都没有就让我塞进去跟主图」） ----------
        # 收 base64 dataURL，落 kitset/<型号>/官网/主图.png —— 下次生成 _manual_overrides
        # 直接当 product.png（跳过 AI 选图）；kind=size 落 尺寸图.png 同理进 sizeimg 链。
        if u.path == "/api/kitmain":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                d = {}
            model = str(d.get("model") or "").strip()
            data = str(d.get("data") or "")
            kind = str(d.get("kind") or "main")
            if not model or not data:
                self._send(400, json.dumps({"ok": False, "msg": "缺 model 或图片数据"}, ensure_ascii=False))
                return
            import re as _re
            m = _re.match(r"data:image/(?:png|jpe?g|webp);base64,(.+)", data, _re.S)
            if not m:
                self._send(400, json.dumps({"ok": False, "msg": "图片数据格式不对"}, ensure_ascii=False))
                return
            try:
                import kitset as _ks
                if kind == "spot1":
                    # 2026-11-23 用户令「没有首图的给我一个按键上传首图」：聚光首图
                    # 直落 kitset/<型号>/聚光/1_首图.jpg + .manual_1 标记——整套重出
                    # 时 gen_style 看到标记就跳过首图槽，不覆盖用户传的图。
                    dd = _ks.spot_dir(model)
                    os.makedirs(dd, exist_ok=True)
                    _stem = "1_首图"
                    for _f in os.listdir(dd):
                        _st, _ex = os.path.splitext(_f)
                        if _st == "1_首图" and _ex.lower() in (".jpg", ".jpeg", ".png"):
                            try:
                                os.remove(os.path.join(dd, _f))
                            except Exception:
                                pass
                    fp = os.path.join(dd, "1_首图.jpg")
                    with open(fp, "wb") as fh:
                        fh.write(base64.b64decode(m.group(1)))
                    try:
                        open(os.path.join(dd, ".manual_1"), "w").write("1")
                    except Exception:
                        pass
                else:
                    dd = os.path.join(_ks.model_dir(model), "官网")
                    os.makedirs(dd, exist_ok=True)
                    _stem = "尺寸图2" if kind == "size2" else ("尺寸图" if kind == "size" else "主图")
                    # 清掉旧变体（主图.jpg/主图_2.png 之类），只留刚传的这张，避免 override 挑错
                    for _f in os.listdir(dd):
                        _st, _ex = os.path.splitext(_f)
                        if _st == _stem or _st.startswith(_stem + "_"):
                            try:
                                os.remove(os.path.join(dd, _f))
                            except Exception:
                                pass
                    fp = os.path.join(dd, _stem + ".png")
                    with open(fp, "wb") as fh:
                        fh.write(base64.b64decode(m.group(1)))
                state.LOG.append("手动塞图：%s ← %s（%d KB）"
                                 % (model, os.path.basename(fp), len(m.group(1)) // 1370))
                # 2026-11-27 ZQD 实锤（用户「上传本地图他不会变」）：上传链漏调
                # _manual_overrides——kitpick（点候选图）落盘后会调，上传却不调，
                # sizeimg.jpg/product.png 还是旧图，选图面板槽位跟出图全不认新图。
                # 补齐：主图→product.png、尺寸图→清洗→sizeimg.jpg，与 kitpick 同口径。
                if kind != "spot1":
                    try:
                        _ks._manual_overrides(_ks.model_dir(model), lambda s: None)
                    except Exception:
                        pass
                self._send(200, json.dumps({"ok": True, "saved": fp}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "msg": str(e)}, ensure_ascii=False))
            return

        # ---------- 清缓存全部重出（2026-11-27 用户令「给我一个按键，清理缓存出的，全部重新出」） ----------
        # 清两处机器缓存：①文案缓存（copy_root 下 *_v*.json，314 个）②每型号缓存
        # （info.json/set.json/cand.json/_state_pick.json/_neg.json/_xs_read_params.json/_cand_ocr.png）。
        # 手选图/上传主图/说明书 PDF/官网欣盛中关村抓取归档图保留——那些是素材不是缓存。
        # 然后后台把 kitset 全部型号 force=1 重出一遍（jobman 进度，可取消）。仅管理号。
        if u.path == "/api/kitflush":
            if state.PERM_STATE.get("role") != "admin":
                self._send(403, json.dumps({"ok": False, "msg": "只有管理号能清缓存重出"},
                                           ensure_ascii=False))
                return
            import jobman as _jm
            # 2026-11-27 用户令「清缓存重出对应到我的生成型号」：带 ?model=X 只清重出这一台
            # （卡片按钮行逐型号按键），不带 = 全部型号（侧栏原按键口径）。
            # 手选图/主图/说明书/抓取归档图一律保留。
            _one = (urllib.parse.parse_qs(u.query).get("model") or [""])[0].strip()
            _kk = ("kitflush:" + _one) if _one else "kitflush"

            def _do():
                import kitset as _kf
                import copywriter as _cw2
                import re as _re

                def _p(s):
                    state.LOG.append("[清缓存重出] " + str(s))
                    _jm.prog(_kk, str(s))
                    if _jm.is_cancelled(_kk):
                        raise _jm.CancelledError("已取消")

                n1 = 0
                try:
                    _cr = _cw2.copy_root()
                    _pfx = ""
                    if _one:
                        _nm = _cw2.norm_model(_one) or _one
                        _pfx = _re.sub(r"[^0-9A-Za-z._-]", "_", _nm)
                    for _f in os.listdir(_cr):
                        if _f.endswith(".json") and "_v" in _f:
                            if _pfx and not _f.startswith(_pfx):
                                continue
                            try:
                                os.remove(os.path.join(_cr, _f))
                                n1 += 1
                            except OSError:
                                pass
                except Exception:
                    pass
                _p("文案缓存已清 %d 个" % n1)
                _FLUSH = ("info.json", "set.json", "cand.json", "_state_pick.json",
                          "_neg.json", "_xs_read_params.json", "_cand_ocr.png")
                if _one:
                    _mdir0 = _kf.model_dir(_one)
                    for _fn in _FLUSH:
                        _p2 = os.path.join(_mdir0, _fn)
                        if os.path.isfile(_p2):
                            try:
                                os.remove(_p2)
                            except OSError:
                                pass
                    _models = [_one]
                    _p("已清 %s 的缓存数据（手选/主图/说明书/抓取图保留）" % _one)
                else:
                    _root = _kf.kit_root()
                    _models = sorted(_d for _d in os.listdir(_root)
                                     if os.path.isdir(os.path.join(_root, _d))
                                     and not _d.startswith("."))
                    for _m in _models:
                        for _fn in _FLUSH:
                            _p2 = os.path.join(_root, _m, _fn)
                            if os.path.isfile(_p2):
                                try:
                                    os.remove(_p2)
                                except OSError:
                                    pass
                    _p("已清 %d 个型号的缓存数据（手选/主图/说明书/抓取图保留）" % len(_models))
                ok, fail = 0, 0
                for _i, _m in enumerate(_models):
                    _p("全部重出 %s（%d/%d）…" % (_m, _i + 1, len(_models)))
                    try:
                        _kf.generate(_m, 1, log=_p)
                        ok += 1
                    except _jm.CancelledError:
                        raise
                    except Exception as _e:
                        fail += 1
                        _p("⚠ %s 重出失败：%s" % (_m, _e))
                return {"ok": True, "models": len(_models), "done": ok, "fail": fail,
                        "msg": "重出完成：%d 成功 / %d 失败" % (ok, fail)}

            if not _jm.submit(_kk, _do, _log=lambda s: state.LOG.append("[清缓存重出] " + s)):
                self._send(200, json.dumps({"ok": False, "busy": True,
                                            "msg": "清缓存重出已经在跑了"}, ensure_ascii=False))
                return
            self._send(200, json.dumps({"ok": True, "started": True, "key": _kk}, ensure_ascii=False))
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
        if u.path == "/api/pubcancel":
            # 取消当前在跑的聚光/素材任务（2026-11-14 用户令「我连取消的按键都没有」）：
            # pubwork 串行槽立取消旗，_job_prog 下一个打点收工。
            import pubwork
            hit = pubwork.job_cancel()
            self._send(200, json.dumps({"ok": True, "cancelled": bool(hit),
                                        "msg": "已请求取消" if hit else "当前没有在跑的任务"},
                                       ensure_ascii=False))
            return

        if u.path in ("/api/pubgen", "/api/pubhero", "/api/pubsel",
                      "/api/pubimport", "/api/pubopen",
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
                        self._send(200, json.dumps({"ok": False, "busy": True, "msg": "已有任务在跑：" + pubwork.busy_msg()}, ensure_ascii=False))
                        return
                    style = str(d.get("style") or "studio")
                    if style not in pubwork.STYLE_DIRS:
                        self._send(400, json.dumps({"ok": False, "msg": "未知风格"}, ensure_ascii=False))
                        return
                    # 2026-11-04 用户令：聚光首图标题=软件条目标题（卡片上那句），
                    # 官网抓取的页面标题（如「大通量前置过滤器」）只是兜底
                    etitle = str(d.get("title") or "").strip()
                    threading.Thread(target=pubwork.gen_style,
                                     args=(model, style, price, ver),
                                     kwargs={"entry_title": etitle,
                                             "only": d.get("only")}, daemon=True).start()
                    self._send(200, json.dumps({"ok": True, "started": True}, ensure_ascii=False))
                    return
                if u.path == "/api/pubhero":
                    if pubwork.job_state()["running"]:
                        self._send(200, json.dumps({"ok": False, "busy": True, "msg": "已有任务在跑：" + pubwork.busy_msg()}, ensure_ascii=False))
                        return
                    threading.Thread(target=pubwork.gen_hero,
                                     args=(model, price, ver), daemon=True).start()
                    self._send(200, json.dumps({"ok": True, "started": True}, ensure_ascii=False))
                    return
                if u.path == "/api/pubsel":
                    rels = [str(x).replace("\\", "/") for x in (d.get("files") or [])
                            if str(x) and ".." not in str(x)]
                    r = pubwork.sel_save(model, rels, sell_price=price, version=ver)
                # 2026-11-27 用户令：/api/pubqa 与 /api/pubauto（AI 成片自检/自动优化）随
                # qa_render 整体下马，端点撤销——前端按钮已删，打过来一律落 404 分支
                elif u.path == "/api/pubimport":
                    r = pubwork.import_imgs(model, d.get("files") or [])
                elif u.path == "/api/pubxy":
                    # 一键发布闲鱼：playwright 半自动，阻塞在后台线程（用户在浏览器点发布）
                    if pubwork.job_state()["running"]:
                        self._send(200, json.dumps({"ok": False, "busy": True, "msg": "已有任务在跑：" + pubwork.busy_msg()}, ensure_ascii=False))
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

        if u.path == "/api/imgclip":
            # 2026-11-17 用户令「复制的是四张，不是拼在一起的」：把图作为文件列表
            # （CF_HDROP）放进系统剪贴板——闲鱼/微信粘贴一次就是四张独立图。
            # body: {"urls": ["/api/kitimg/<型号>/蓝底/img_N.jpg" | "/pubimg/..."]}。
            # 图片地址解析口径与 api_get 的 kitimg/pubimg 完全一致，只许读 kitset 内。
            try:
                ln = int(self.headers.get("Content-Length") or 0)
                d = json.loads(self.rfile.read(ln).decode("utf-8") or "{}")
                urls = [str(x) for x in (d.get("urls") or []) if str(x).strip()]
                import kitset
                import pubwork
                fps = []
                for pu in urls[:4]:
                    core = urllib.parse.unquote(pu.split("?")[0])
                    if core.startswith("/api/kitimg/"):
                        base = os.path.abspath(kitset.kit_root())
                        fp = os.path.abspath(os.path.join(
                            base, core[len("/api/kitimg/"):]))
                        if not fp.startswith(base):
                            raise ValueError("越界路径")
                    elif core.startswith("/pubimg/"):
                        parts = core[len("/pubimg/"):].split("/")
                        if len(parts) >= 2 and parts[1] in (pubwork.SPOT_SUB, "AI6张_聚光"):
                            if parts[1] != pubwork.SPOT_SUB:
                                parts[1] = pubwork.SPOT_SUB
                            base = kitset.model_dir(parts[0])
                            parts = parts[1:]
                        else:
                            base = pubwork.sroot()
                        fp = os.path.abspath(os.path.join(base, *parts))
                        if not fp.startswith(os.path.abspath(base)):
                            raise ValueError("越界路径")
                    else:
                        raise ValueError("不认识的图片地址")
                    if not os.path.isfile(fp):
                        raise ValueError("图片不在本机: " + os.path.basename(fp))
                    fps.append(fp)
                if not fps:
                    raise ValueError("没拿到图片")

                def _clip_files(paths):
                    import struct
                    import win32clipboard
                    import win32con
                    # DROPFILES 头：pFiles=20、pt=(0,0)、fNC=0、fWide=1（UTF-16 文件名）
                    header = struct.pack("Iiiii", 20, 0, 0, 0, 1)
                    payload = "".join(p + "\0" for p in paths) + "\0"
                    data = header + payload.encode("utf-16-le") + b"\0\0"
                    win32clipboard.OpenClipboard()
                    try:
                        win32clipboard.EmptyClipboard()
                        win32clipboard.SetClipboardData(win32con.CF_HDROP, data)
                    finally:
                        win32clipboard.CloseClipboard()

                _clip_files(fps)
                self._send(200, json.dumps(
                    {"ok": True, "n": len(fps)}, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps(
                    {"ok": False, "msg": str(e)}, ensure_ascii=False))
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
