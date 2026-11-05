#!/bin/bash
# =====================================================================
# 型号查询 · Mac 修复并打开（2026-11-05）
#
# 什么时候用：
#   · 双击 App 完全没反应、连窗口都不出来
#   · 图标在 Dock 跳一下就没
#   · 提示「已损坏，应移到废纸篓」/「无法验证开发者」
#
# 这个脚本做四件事，全部只动 App 自己，不碰你的文件：
#   1) 去掉下载隔离标记（com.apple.quarantine）
#   2) 把误生成在 App 包里的「图片库」搬到 ~/Pictures（这一步是修签名的关键）
#   3) 重新做一次本机 ad-hoc 签名
#   4) 打开 App
#
# 如果被系统拦住不让运行：右键点这个文件 → 打开 → 再点「打开」。
# 或者打开「终端」，把下面这行粘进去回车（路径按实际改）：
#   bash ~/Downloads/修复并打开.command
# =====================================================================
set -u

LOG="$HOME/Desktop/型号查询-修复日志.txt"
: > "$LOG" 2>/dev/null || LOG="/tmp/型号查询-修复日志.txt"
: > "$LOG"

say() { printf '%s\n' "$*" | tee -a "$LOG"; }

say "======================================================"
say " 型号查询 · Mac 修复  $(date '+%Y-%m-%d %H:%M:%S')"
say " 系统：$(sw_vers -productName) $(sw_vers -productVersion)  芯片：$(uname -m)"
say "======================================================"
say ""

# ---------- 1. 找 App ----------
APP=""
HERE="$(cd "$(dirname "$0")" 2>/dev/null && pwd)"
for c in "$HERE/ModelLookup.app" "/Applications/ModelLookup.app" "$HOME/Applications/ModelLookup.app"; do
    if [ -d "$c" ]; then APP="$c"; break; fi
done
if [ -z "$APP" ]; then
    say "✗ 没找到 ModelLookup.app。"
    say "  请先把「型号查询」拖到「应用程序」文件夹，再双击本脚本。"
    say ""
    read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi
say "App 位置：$APP"

# 在只读的 DMG 里直接跑 → 必须先进应用程序
case "$APP" in
    /Volumes/*)
        say ""
        say "✗ 你现在是直接在下载的磁盘映像里双击 App。"
        say "  请先把 ModelLookup.app 拖到「应用程序」文件夹，再运行本脚本。"
        say ""
        read -n 1 -s -r -p "按任意键关闭…"; exit 1
        ;;
esac

BIN="$APP/Contents/MacOS/ModelLookup"
if [ ! -f "$BIN" ]; then
    say "✗ 这个 App 不完整，找不到 $BIN"
    say "  请删掉它，重新从 Releases 下载安装包。"
    read -n 1 -s -r -p "按任意键关闭…"; exit 1
fi

# ---------- 2. 去隔离标记 ----------
say ""
say "[1/4] 去掉下载隔离标记…"
xattr -dr com.apple.quarantine "$APP" 2>>"$LOG" && say "      ok" || say "      （没有隔离标记，跳过）"

# ---------- 3. 搬出 App 包里的「图片库」（修签名关键） ----------
say ""
say "[2/4] 检查 App 包里有没有被误写进去的「图片库」…"
STRAY="$APP/Contents/MacOS/图片库"
DEST="$HOME/Pictures/型号查询图片库"
if [ -d "$STRAY" ]; then
    say "      发现 $STRAY"
    mkdir -p "$DEST"
    for item in "$STRAY"/*; do
        [ -e "$item" ] || continue
        name="$(basename "$item")"
        if [ -e "$DEST/$name" ]; then
            mv "$item" "$DEST/${name}__包内备份" && say "      搬出 $name（目标已有，存为 ${name}__包内备份）"
        else
            mv "$item" "$DEST/$name" && say "      搬出 $name"
        fi
    done
    rmdir "$STRAY" 2>/dev/null && say "      已清空并删除「图片库」目录 ← 签名 seal 恢复" \
        || say "      ⚠ 目录没删干净，请手动在 Finder 里删除：$STRAY"
else
    say "      没有（正常）"
fi

# ---------- 4. 重新签名 ----------
say ""
say "[3/4] 重新做本机签名…"
codesign --force --deep --sign - "$APP" 2>>"$LOG" && say "      签名完成" || say "      ⚠ 签名失败，见日志"
if codesign --verify --deep --strict --verbose=2 "$APP" >>"$LOG" 2>&1; then
    say "      校验通过 ✅"
else
    say "      ⚠ 校验没过，见日志（多半是包里还有别的东西，建议重新下载安装包）"
fi

# ---------- 5. 打开 ----------
say ""
say "[4/4] 打开 App…"
open "$APP" 2>>"$LOG" && say "      已发送启动指令" || say "      ✗ open 失败，见日志"
say ""
say "如果还是没反应，把这个日志文件发给作者：$LOG"
say "（窗口 10 秒后自动关闭，也可以先别关，看看上面有没有红字提示）"
say ""
sleep 10
