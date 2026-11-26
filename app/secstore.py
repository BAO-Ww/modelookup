# -*- coding: utf-8 -*-
"""本机数据落盘加密（2026-11-26 用户令「在他本地的那个加密码」）：
子用户盘上不能有明文原价。三件 xlsx（data/price/subsidy）+ payload.json
落盘一律加密（KDX1 容器），读取时内存解密、不留明文临时文件；
读到旧明文顺手就地转加密（升级后第一次读 = 无感迁移）。
钥匙 = 本机 MachineGuid + 程序内 salt 派生 —— 每台机器钥匙不同，
把文件拷到别的电脑也解不开。防翻文件夹/拷文件，不防逆向（边界已向用户说明）。"""
import hashlib
import io
import os

MAGIC = b"KDX1"
_SALT = b"modelookup-kdx-2026-11-26-tingjiabaobao"


def _machine_key():
    """本机钥匙：Windows MachineGuid；拿不到就用固定回退（加密链路绝不能挂）。"""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography") as k:
            guid, _t = winreg.QueryValueEx(k, "MachineGuid")
        return str(guid).strip().encode("utf-8") or b"empty-guid"
    except Exception:
        return b"no-guid-fallback"


def _keystream(n, key):
    """sha256 计数器模式流密钥（解密同一条流，XOR 对称）。"""
    out = bytearray()
    i = 0
    while len(out) < n:
        out += hashlib.sha256(key + _SALT + b"#" + str(i).encode("ascii")).digest()
        i += 1
    return bytes(out[:n])


def enc(raw):
    """明文字节 → KDX1 容器（魔数4B + 原长4B + 异或体）。已是密文则原样返回。"""
    if raw[:4] == MAGIC:
        return raw
    key = _machine_key()
    body = bytes(a ^ b for a, b in zip(raw, _keystream(len(raw), key)))
    return MAGIC + len(raw).to_bytes(4, "big") + body


def dec(data):
    """KDX1 容器 → 明文；非本容器（旧明文/别的文件）原样返回，绝不抛。"""
    try:
        if data[:4] != MAGIC:
            return data
        key = _machine_key()
        n = int.from_bytes(data[4:8], "big")
        body = data[8:8 + n]
        return bytes(a ^ b for a, b in zip(body, _keystream(n, key)))
    except Exception:
        return data


def read_bytes(path):
    """读文件并解密；读到旧明文顺手就地转加密（升级无感迁移，失败不影响本次读取）。
    调用方只有我们自己的四个缓存点（3 件 xlsx + payload.json），全转都安全。"""
    with open(path, "rb") as f:
        raw = f.read()
    data = dec(raw)
    if raw[:4] != MAGIC and raw:
        try:
            write_bytes(path, data)          # 明文缓存 → 立刻洗成密文
        except Exception:
            pass
    return data


def write_bytes(path, data):
    """加密落盘（文件名不变，Excel 双击只会提示文件损坏）。"""
    tmp = str(path) + ".kdx.tmp"
    with open(tmp, "wb") as f:
        f.write(enc(data))
    os.replace(tmp, path)


def open_xlsx(path):
    """给 openpyxl.load_workbook 用的内存流（解密后），全程不留明文临时文件。"""
    return io.BytesIO(read_bytes(path))
