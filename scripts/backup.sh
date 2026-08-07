#!/usr/bin/env bash
# ============================================================
# 数据卷备份/恢复脚本(发布清单: 数据卷备份/恢复演练)
# 备份:   ./scripts/backup.sh [输出目录=./backups]
# 恢复:   ./scripts/backup.sh --restore <备份文件>
# 数据内容: SQLite + CSV 交易日志 + K线缓存(data/ 目录)
# ============================================================
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DATA_DIR="${DATA_DIR:-$ROOT/data}"
TS="$(date +%Y%m%d_%H%M%S)"

# tar.exe(Git Bash)需要 Windows 格式路径; 纯 Linux 环境用原路径
to_win() {
  local p="$1"
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$p"
  elif [[ "$p" =~ ^/([a-zA-Z])/(.*)$ ]]; then
    echo "${BASH_REMATCH[1],,}:/${BASH_REMATCH[2]}"
  else
    echo "$p"
  fi
}
ROOT_TAR="$(to_win "$ROOT")"

if [ "${1:-}" = "--restore" ]; then
  FILE="${2:-}"
  if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
    echo "用法: $0 --restore <备份文件.tar.gz>" >&2
    exit 1
  fi
  echo "⚠️  恢复将覆盖现有 data/ 目录: $DATA_DIR"
  read -r -p "确认恢复? (y/N) " ans
  [ "$ans" = "y" ] || exit 0
  mkdir -p "$DATA_DIR"
  tar -xzf "$FILE" -C "$ROOT_TAR"
  echo "恢复完成: $FILE -> $DATA_DIR"
  exit 0
fi

DEST="${1:-$ROOT/backups}"
mkdir -p "$DEST"
DEST_TAR="$(to_win "$DEST")"
tar -czf "$DEST_TAR/momentum-trader-$TS.tar.gz" -C "$ROOT_TAR" "$(basename "$DATA_DIR")"
echo "备份完成: $DEST/momentum-trader-$TS.tar.gz"
echo "恢复方法: $0 --restore $DEST/momentum-trader-$TS.tar.gz"
