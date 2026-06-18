#!/usr/bin/env bash
# deploy/update.sh — 更新程式碼並重啟服務
#
# 用法：
#   sudo bash deploy/update.sh
#
# 執行步驟：
#   1. git pull 拉最新版本
#   2. 更新 Python 套件
#   3. 重啟 systemd 服務
# ─────────────────────────────────────────────────────────
set -euo pipefail

G='\033[0;32m' Y='\033[1;33m' R='\033[0;31m' BOLD='\033[1m' N='\033[0m'
info() { echo -e "${G}[✓]${N} $*"; }
warn() { echo -e "${Y}[!]${N} $*"; }
err()  { echo -e "${R}[✗]${N} $*" >&2; }

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

echo -e "${BOLD}=== Hyperliquid Copy Trader — 更新程序 ===${N}"
echo "  專案路徑: $PROJECT_DIR"
echo ""

[[ $EUID -eq 0 ]] || { err "請以 sudo 執行：sudo bash deploy/update.sh"; exit 1; }

# ── 1. 拉最新程式碼 ───────────────────────────────────────
echo "[1/3] git pull..."
if git -C "$PROJECT_DIR" rev-parse --git-dir &>/dev/null; then
    BEFORE=$(git -C "$PROJECT_DIR" rev-parse --short HEAD)
    git -C "$PROJECT_DIR" pull
    AFTER=$(git -C "$PROJECT_DIR" rev-parse --short HEAD)
    if [[ "$BEFORE" == "$AFTER" ]]; then
        warn "程式碼無變更（已是最新版）"
    else
        info "已更新：$BEFORE → $AFTER"
    fi
else
    warn "非 git 倉庫，跳過 pull（直接更新套件）"
fi

# ── 2. 更新 Python 套件 ───────────────────────────────────
echo "[2/3] 更新 Python 套件..."
if [[ -f "$VENV_DIR/bin/pip" ]]; then
    "$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" --quiet
    info "套件更新完成"
else
    warn "找不到 venv，請先執行 sudo bash deploy/setup.sh"
    exit 1
fi

# ── 3. 重啟服務 ───────────────────────────────────────────
echo "[3/3] 重啟服務..."
systemctl restart hl-copytrader
sleep 2

echo ""
systemctl status hl-copytrader --no-pager --lines=8 || true
echo ""
info "更新完成！使用 journalctl -u hl-copytrader -f 查看即時 log"
