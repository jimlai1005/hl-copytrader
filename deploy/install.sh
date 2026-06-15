#!/usr/bin/env bash
# 在雲端主機上執行此腳本，完成服務安裝與啟動
# 使用方式：sudo bash deploy/install.sh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE_FILE="$PROJECT_DIR/deploy/hl-copytrader.service"
SYSTEMD_FILE="/etc/systemd/system/hl-copytrader.service"
SERVICE_USER="${SUDO_USER:-ubuntu}"

echo "=== Hyperliquid Copy Trader 安裝程序 ==="
echo "專案路徑: $PROJECT_DIR"
echo "服務使用者: $SERVICE_USER"

# 確認 .env 存在
if [ ! -f "$PROJECT_DIR/.env" ]; then
    echo "[錯誤] 找不到 .env，請先複製 .env.example 並填入設定"
    exit 1
fi

# 安裝 Python 套件
echo "[1/4] 安裝 Python 套件..."
pip3 install -r "$PROJECT_DIR/requirements.txt" --quiet

# 更新 service 檔中的路徑與使用者
echo "[2/4] 產生 systemd service 檔..."
sed \
    -e "s|/home/ubuntu/projects/hl-copytrader|$PROJECT_DIR|g" \
    -e "s|User=ubuntu|User=$SERVICE_USER|g" \
    -e "s|Group=ubuntu|Group=$SERVICE_USER|g" \
    "$SERVICE_FILE" > "$SYSTEMD_FILE"

# 重新載入並啟用服務
echo "[3/4] 啟用 systemd 服務..."
systemctl daemon-reload
systemctl enable hl-copytrader
systemctl restart hl-copytrader

# 顯示狀態
echo "[4/4] 服務狀態："
systemctl status hl-copytrader --no-pager

echo ""
echo "=== 安裝完成 ==="
echo "常用指令："
echo "  journalctl -u hl-copytrader -f          # 即時看 log"
echo "  systemctl status hl-copytrader           # 服務狀態"
echo "  systemctl stop hl-copytrader             # 暫停跟單"
echo "  systemctl restart hl-copytrader          # 重啟"
