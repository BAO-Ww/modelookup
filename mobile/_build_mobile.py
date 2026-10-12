# -*- coding: utf-8 -*-
"""手机版构建器：现行模板（app/assets/template.html）+ payload 快照 + 手机增强层。
产物：自包含 HTML（APK 的 www 页面 / 浏览器样机通用）。
⚠ 模板文件只读，零改动；本脚本可反复重跑。

用法:
  python _build_mobile.py                       # 本机默认（LOCALAPPDATA 最新 payload → build/ 样机）
  python _build_mobile.py --payload P --out O   # CI 模式（指定快照与输出）"""
import argparse
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TPL = os.path.join(ROOT, "app", "assets", "template.html")
MOB = os.path.dirname(os.path.abspath(__file__))

MARK = "var DATA = /*__DATA__*/null;"
GUARD = "if (location.protocol !== 'http:') return;"
GUARD_NEW = ("if (location.protocol !== 'http:' && location.protocol !== 'https:') return;"
             "/* 手机版：Capacitor 是 https://localhost，也要启用状态轮询——"
             "云端库存靠 rev→reloadAll 注入模板（mobile.js 拦截层供数据） */")


def build(payload_path, out_path, split=False):
    tpl = io.open(TPL, encoding="utf-8").read()
    if MARK not in tpl:
        sys.exit("模板里找不到 DATA 占位（%r）——现行模板变过？检查 app/assets/template.html" % MARK)
    if GUARD not in tpl:
        sys.exit("模板里找不到 protocol 守卫（%r）——现行模板变过？检查 app/assets/template.html" % GUARD)
    pay = json.load(io.open(payload_path, encoding="utf-8"))
    rows = len(pay.get("rows") or [])
    imgs = len(pay.get("imgs") or {})
    print("payload=%s：%d rows / %d imgs / built=%s"
          % (os.path.basename(payload_path), rows, imgs, (pay.get("meta") or {}).get("built")))

    raw = json.dumps(pay, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    # 关键设计（2026-10-11）：DATA 置空 → 启动走桌面 exe 同款原生路径：
    #   ensureAuth → fetch /api/data（拦截层供 window.__ML_PAYLOAD）
    #              → fetch /api/stock（拦截层供云端 /stockcloud 实时库存）
    #   → Promise.all → 内部 STOCKDATA 正确赋值 → start() 一次性带库存渲染。
    # payload 只存一份（window.__ML_PAYLOAD）；拦截层不挑协议（file:///https 均可）。
    html = tpl.replace(MARK, "var DATA = null;", 1)
    html = html.replace(GUARD, GUARD_NEW, 1)

    css = io.open(os.path.join(MOB, "mobile.css"), encoding="utf-8").read()
    js = io.open(os.path.join(MOB, "mobile.js"), encoding="utf-8").read()

    if "name=\"viewport\"" not in html and "name='viewport'" not in html:
        html = html.replace("<head>", '<head>\n<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">', 1)
        print("已补 viewport meta")

    inject = ("<script id=\"ml-payload\">window.__ML_PAYLOAD = " + raw + ";</script>\n"
              "<style id=\"ml-mobile-css\">\n" + css + "\n</style>\n"
              "<script id=\"ml-mobile-js\">\n" + js + "\n</script>\n")
    if split:
        # 拆分版（在线预览）：页面不含数据本体，秒开；rows/imgs 分文件后台加载
        # （mobile.js 先取 data/rows.json 渲染，imgs.json 到货后 bump rev 带图重渲染）
        inject = inject.replace('<script id="ml-payload">window.__ML_PAYLOAD = ' + raw + ';</script>\n', '')
        out_dir = os.path.dirname(os.path.abspath(out_path))
        data_dir = os.path.join(out_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        rows_obj = {k: v for k, v in pay.items() if k != "imgs"}
        rows_obj["imgs"] = {}
        with io.open(os.path.join(data_dir, "rows.json"), "w", encoding="utf-8") as f:
            json.dump(rows_obj, f, ensure_ascii=False, separators=(",", ":"))
        with io.open(os.path.join(data_dir, "imgs.json"), "w", encoding="utf-8") as f:
            json.dump({"imgs": pay.get("imgs") or {}}, f, ensure_ascii=False, separators=(",", ":"))
        print("data/rows.json %.2f MB + data/imgs.json %.2f MB" % (
            os.path.getsize(os.path.join(data_dir, "rows.json")) / 1048576.0,
            os.path.getsize(os.path.join(data_dir, "imgs.json")) / 1048576.0))
    html = html.replace("</head>", inject + "</head>", 1)

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with io.open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("ARTIFACT: %s (%.2f MB)" % (out_path, os.path.getsize(out_path) / 1048576.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", default=os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "ModelLookup", "payload.json"))
    ap.add_argument("--out", default=os.path.join(MOB, "build", "型号查询-手机版-样机.html"))
    ap.add_argument("--split", action="store_true",
                    help="拆分版：页面不含数据，data/rows.json+imgs.json 后台加载（在线预览用）")
    a = ap.parse_args()
    if not os.path.isfile(a.payload):
        sys.exit("payload not found: %s" % a.payload)
    build(a.payload, a.out, split=a.split)


if __name__ == "__main__":
    main()
