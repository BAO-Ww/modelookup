# -*- coding: utf-8 -*-
"""通用读表地基（2026-10-11 拆分一期二级细拆：从 main.py 原样搬出，纯搬运零行为变化）。
表头定位 / 列名模糊匹配 / 单元格取值——全部纯函数，任何解析器共用。
依赖方向：normalize → parsing（只依赖标准库 + normalize）。"""
import re

from normalize import num, sx


def _is_placeholder_label(n):
    """表头格里是纯数字（日期序列号 46299）或日期字符串 → 视为占位，不是列名。"""
    t = (n or "").strip()
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return True
    if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}( \d{1,2}:\d{2}(:\d{2})?)?", t):
        return True
    return False


def _nk(x):
    """列名归一：去掉所有空白（「价格 含税」↔「价格含税」）。"""
    return re.sub(r"\s+", "", str(x or ""))


def header_map(rows, need=("型号",), scan=4):
    """在前 scan 行里找表头行：返回 (行号0基, {列名:列号0基})。

    表头格里为空 或 是日期/数字占位 的列，从紧下一行同列补名字。
    """
    for ri in range(min(scan, len(rows))):
        names = [sx(x) for x in (rows[ri] or [])]
        if not all(n in names for n in need):
            continue
        nxt = rows[ri + 1] if ri + 1 < len(rows) else []
        col = {}
        for i, n in enumerate(names):
            if n is None or _is_placeholder_label(n):
                v2 = sx(nxt[i]) if i < len(nxt) else None
                if v2 and not _is_placeholder_label(v2):
                    n = v2
            if n:
                col.setdefault(n, i)
        return ri, col
    raise RuntimeError("找不到表头行 need=%s" % (need,))


def col_of(col, *cands):
    """列名模糊匹配（忽略空格），先精确、后包含。

    实际表头里坑不少：「价格 含税」中间有空格；「利润」那列真名是
    「除去提货价与平台扣点后利润，考虑是否可做活动」；「备注」重复出现两次。
    """
    for c in cands:
        for k, v in col.items():
            if _nk(k) == _nk(c):
                return v
    for c in cands:
        for k, v in col.items():
            if _nk(c) and _nk(c) in _nk(k):
                return v
    return None


def rows_of(ws):
    return [list(r) for r in ws.iter_rows(values_only=True)]


def sc(i, r):
    return sx(r[i]) if (i is not None and i < len(r)) else None


def nc(i, r):
    return num(r[i]) if (i is not None and i < len(r)) else None
