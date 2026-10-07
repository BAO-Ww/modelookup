# -*- coding: utf-8 -*-
"""_payload_kdocs.json + template_kdocs.html → 单文件查询页 HTML。

用法: python build_kdocs.py [输出路径]
默认输出到本目录「型号查询-公布版.html」。
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def build(out=None, payload=None, tpl=None):
    payload_path = payload or os.path.join(HERE, "_payload_kdocs.json")
    tpl_path = tpl or os.path.join(HERE, "template_kdocs.html")
    if not os.path.isfile(payload_path):
        raise SystemExit("缺 payload：%s（先跑 parse_kdocs.py）" % payload_path)

    with io.open(payload_path, encoding="utf-8") as f:
        data = json.load(f)
    with io.open(tpl_path, encoding="utf-8") as f:
        tpl_src = f.read()

    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    raw = raw.replace("</", "<\\/")          # 防止数据里出现 </script> 提前闭合
    # ⚠ 占位符要连后面的 null 一起替换：不替换时它必须仍是合法 JS
    #   （模板里写的是 `var DATA = /*__DATA__*/null;`，exe 版就拿它当空值 → 走 HTTP 取数）
    if "/*__DATA__*/null" not in tpl_src:
        raise SystemExit("模板里找不到 /*__DATA__*/null 占位")
    html = tpl_src.replace("/*__DATA__*/null", raw, 1)

    out = out or os.path.join(HERE, "型号查询-公布版.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("→ %s" % out)
    print("   型号 %d 个 / 图 %d 张 / 体积 %.2f MB" % (
        len(data.get("rows", [])), len(data.get("imgs", {})),
        len(html.encode("utf-8")) / 1048576.0))

    # 同一个模板也供 exe 版用（exe 读的是「未注入数据」的原始模板 + 单独的 payload.json）
    adir = os.path.join(HERE, "app", "assets")
    if os.path.isdir(adir):
        with io.open(os.path.join(adir, "template.html"), "w", encoding="utf-8") as f:
            f.write(tpl_src)                   # ← 原始模板，保留 /*__DATA__*/ 占位
        with io.open(os.path.join(adir, "payload.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print("   app/assets/template.html + payload.json 已同步")
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    build(args[0] if args else None)
