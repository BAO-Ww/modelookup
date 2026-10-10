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


def build(payload_path, out_path):
    tpl = io.open(TPL, encoding="utf-8").read()
    if MARK not in tpl:
        sys.exit("模板里找不到 DATA 占位（%r）——现行模板变过？检查 app/assets/template.html" % MARK)
    pay = json.load(io.open(payload_path, encoding="utf-8"))
    rows = len(pay.get("rows") or [])
    imgs = len(pay.get("imgs") or {})
    print("payload=%s：%d rows / %d imgs / built=%s"
          % (os.path.basename(payload_path), rows, imgs, (pay.get("meta") or {}).get("built")))

    raw = json.dumps(pay, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = tpl.replace(MARK, "var DATA = " + raw + ";", 1)

    css = io.open(os.path.join(MOB, "mobile.css"), encoding="utf-8").read()
    js = io.open(os.path.join(MOB, "mobile.js"), encoding="utf-8").read()

    if "name=\"viewport\"" not in html and "name='viewport'" not in html:
        html = html.replace("<head>", '<head>\n<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">', 1)
        print("已补 viewport meta")

    inject = ("<style id=\"ml-mobile-css\">\n" + css + "\n</style>\n"
              "<script id=\"ml-mobile-js\">\n" + js + "\n</script>\n")
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
    a = ap.parse_args()
    if not os.path.isfile(a.payload):
        sys.exit("payload not found: %s" % a.payload)
    build(a.payload, a.out)


if __name__ == "__main__":
    main()
