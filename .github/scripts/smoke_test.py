# -*- coding: utf-8 -*-
"""smoke_test.py — 发版前冒烟自测（2026-10-24，用户拍板「都弄」第 13 条）
两层断言，全部离线（不起 webview、不连云端）：
  ① 打包完备性：ModelLookup.spec / ModelLookup-mac.spec 的 datas 引用文件全部存在
     —— spec 引 assets 素材漏进 _push.py 白名单 = CI PyInstaller 直接炸（10-17 实锤），
     这里把那类事故拦在推送之前；
  ② 本地服务 API 冒烟：临时 LOCALAPPDATA 隔离环境起 server（无 webview），断言
     /api/ping、/api/state、/api/data 的状态码与结构 —— 改挂 API/改状态字段后的回归。
用法：
    python .github/scripts/smoke_test.py            # 全部
    python .github/scripts/smoke_test.py --spec     # 只跑打包完备性（CI 快速用）
推送接线：_push.py 推送前自动跑本脚本，非零退出 = 拦下不推。
"""
import ast
import json
import os
import re
import shutil
import sys
import tempfile
import traceback
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FAIL = []


def check(name, ok, detail=""):
    print("  %s %s%s" % ("✅" if ok else "❌", name, ("  " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


# ---------------------------------------------------------------- ① spec datas 完备性
def spec_datas_ok(spec_path):
    print("== 打包完备性：%s ==" % os.path.basename(spec_path))
    src = open(spec_path, encoding="utf-8").read()
    # 抽 datas 里 os.path.join(A, "x"[, "y"...]) 的相对路径段
    pairs = re.findall(r"\(\s*os\.path\.join\(A,\s*([^\)]+)\)\s*,\s*\"[^\"]+\"\s*\)", src)
    base = os.path.join(os.path.dirname(spec_path), "assets")   # A = app/assets
    n = 0
    for grp in pairs:
        parts = re.findall(r"\"([^\"]+)\"", grp)
        if not parts:
            continue
        rel = os.path.join(*parts)
        full = os.path.join(base, rel)
        n += 1
        if not os.path.isfile(full):
            check("spec 引用文件存在：%s" % rel, False, "缺失！CI PyInstaller 会炸")
        else:
            check("spec 引用文件存在：%s" % rel, True)
    check("spec datas 共解析 %d 个文件引用" % n, n > 0)
    # spec 里 from import 的 app 模块（hiddenimports）在仓库里都存在
    for m in re.findall(r"[\"']([a-z_0-9]+)[\"'](?=\s*[,\]])", src.split("hiddenimports")[1][:4000]) if "hiddenimports" in src else []:
        p = os.path.join(base, m + ".py")
        if os.path.isfile(os.path.join(base, m + ".py")) or m in ("json",):
            continue
    return True


def push_whitelist_ok():
    print("== 推送白名单 vs spec 引用 ==")
    wl = open(os.path.join(ROOT, "_push.py"), encoding="utf-8").read()
    for spec in ("ModelLookup.spec", "ModelLookup-mac.spec"):
        sp = open(os.path.join(ROOT, "app", spec), encoding="utf-8").read()
        for grp in re.findall(r"\(\s*os\.path\.join\(A,\s*([^\)]+)\)\s*,\s*\"[^\"]+\"\s*\)", sp):
            parts = re.findall(r"\"([^\"]+)\"", grp)
            if not parts:
                continue
            rel = "app/assets/" + "/".join(parts[0:]) if parts[0] != "pei" or len(parts) < 2 else "app/assets/" + "/".join(parts)
            if ('"%s"' % rel) not in wl:
                check("白名单含 %s（%s 引用）" % (rel, spec), False, "没进 _push.py 白名单，远端 CI 会缺文件")
            else:
                check("白名单含 %s" % rel, True)


# ---------------------------------------------------------------- ② 本地服务 API 冒烟
def api_smoke():
    print("== 本地服务 API 冒烟（隔离环境） ==")
    work = tempfile.mkdtemp(prefix="mlsmoke_")
    old_env = os.environ.get("LOCALAPPDATA")
    os.environ["LOCALAPPDATA"] = work
    sys.path.insert(0, os.path.join(ROOT, "app"))
    try:
        import server as _server
        httpd, port = _server.start_server()

        def get(path, timeout=30):
            with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
                return r.status, json.loads(r.read().decode("utf-8", "ignore"))

        st, j = get("/api/ping")
        check("GET /api/ping -> 200", st == 200 and j.get("app") == "ModelLookup", str(j)[:60])

        st, j = get("/api/state")
        check("GET /api/state -> 200 且带 status/rev/cdp_on",
              st == 200 and "status" in j and "rev" in j and "cdp_on" in j,
              "status=%s rev=%s" % (j.get("status"), j.get("rev")))

        st, j = get("/api/data", timeout=120)
        rows_ok = st == 200 and isinstance(j.get("rows"), list)
        check("GET /api/data -> 200 且 rows 是数组", rows_ok,
              "rows=%s" % (len(j.get("rows") or [])))
        meta = j.get("meta") or {}
        check("payload meta 带 stock_rules（2026-10-24 口径单源）",
              isinstance(meta.get("stock_rules"), dict) and bool(meta["stock_rules"].get("ext_free_brands")),
              str((meta.get("stock_rules") or {}).get("ext_free_brands"))[:60])

        # 模板关键元素（改 UI 挖掉关键横幅 id 的回归）
        tpl = open(os.path.join(ROOT, "app", "assets", "template.html"), encoding="utf-8").read()
        for eid in ("lossalert", "dataalert", "pubwarn", "yyalert", "stickhead"):
            check("模板含 #%s" % eid, ('id="%s"' % eid) in tpl)

        httpd.shutdown()
    except Exception as e:
        check("本地服务冒烟", False, "%r" % e)
        traceback.print_exc()
    finally:
        if old_env is not None:
            os.environ["LOCALAPPDATA"] = old_env
        shutil.rmtree(work, ignore_errors=True)


def main():
    only_spec = "--spec" in sys.argv
    print("ModelLookup 发版冒烟：%s" % ROOT)
    for spec in ("ModelLookup.spec", "ModelLookup-mac.spec"):
        spec_datas_ok(os.path.join(ROOT, "app", spec))
    push_whitelist_ok()
    if not only_spec:
        api_smoke()
    print("")
    if FAIL:
        print("❌ 冒烟未过（%d 项）：%s" % (len(FAIL), "；".join(FAIL[:6])))
        return 1
    print("✅ 冒烟全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
