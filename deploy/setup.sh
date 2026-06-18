#!/usr/bin/env bash
# deploy/setup.sh — Hyperliquid Copy Trader 一鍵安裝
#
# 用法（新機器首次安裝）：
#   git clone <repo> hl-copytrader && cd hl-copytrader
#   sudo bash deploy/setup.sh
#
# 可重複執行（idempotent）：
#   - .env 已存在 → 跳過精靈，不覆蓋
#   - venv 已存在 → 只更新套件
#   - 服務已啟動 → 重啟
# ─────────────────────────────────────────────────────────
set -euo pipefail

# ── 顏色 ─────────────────────────────────────────────────
R='\033[0;31m' G='\033[0;32m' Y='\033[1;33m'
B='\033[0;34m' BOLD='\033[1m' N='\033[0m'

info()  { echo -e "${G}[✓]${N} $*"; }
warn()  { echo -e "${Y}[!]${N} $*"; }
err()   { echo -e "${R}[✗]${N} $*" >&2; }
step()  { echo -e "\n${B}${BOLD}─── $* ───${N}"; }
ask()   { echo -e "${Y}►${N} $*"; }

# ── 路徑 ─────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
ENV_FILE="$PROJECT_DIR/.env"
ENV_EXAMPLE="$PROJECT_DIR/.env.example"
SERVICE_TPL="$PROJECT_DIR/deploy/hl-copytrader.service"
SYSTEMD_DST="/etc/systemd/system/hl-copytrader.service"
SERVICE_USER="${SUDO_USER:-${USER:-ubuntu}}"

# ─────────────────────────────────────────────────────────
echo -e "${BOLD}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║     Hyperliquid Copy Trader — 一鍵安裝程序           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${N}"
echo "  專案路徑    : $PROJECT_DIR"
echo "  服務使用者  : $SERVICE_USER"
echo "  Python venv : $VENV_DIR"
echo ""

# ── 1. 環境檢查 ───────────────────────────────────────────
step "1/6  環境檢查"

[[ $EUID -eq 0 ]] || { err "請以 sudo 執行：sudo bash deploy/setup.sh"; exit 1; }

PYTHON_BIN=$(command -v python3 2>/dev/null || true)
[[ -n "$PYTHON_BIN" ]] || {
    err "找不到 python3，請先安裝：sudo apt install python3 python3-pip python3-venv"
    exit 1
}

PYVER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYMAJ=$(echo "$PYVER" | cut -d. -f1)
PYMIN=$(echo "$PYVER" | cut -d. -f2)
{ [[ "$PYMAJ" -ge 3 ]] && [[ "$PYMIN" -ge 8 ]]; } || {
    err "需要 Python 3.8+，目前版本：$PYVER"
    exit 1
}
info "Python $PYVER"

# 確保 venv 模組可用
"$PYTHON_BIN" -m venv --help &>/dev/null || {
    warn "缺少 python3-venv，正在安裝..."
    apt-get install -y python3-venv python3-pip
}
info "python3-venv 可用"

# ── 2. Python venv ────────────────────────────────────────
step "2/6  建立 Python 虛擬環境"

if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
    info "已建立 venv"
else
    info "venv 已存在，僅更新套件"
fi

"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" --quiet
info "Python 套件安裝完成"

# ── 3. 設定 .env ──────────────────────────────────────────
step "3/6  設定 .env"

# 讀取單一欄位：read_field "KEY" "說明" "預設值" [secret]
read_field() {
    local key="$1" desc="$2" default="$3" secret="${4:-}"
    if [[ -n "$default" ]]; then
        ask "$desc"
        printf "   (預設: %s) > " "$default"
    else
        ask "$desc"
        printf "   > "
    fi
    local val
    if [[ "$secret" == "secret" ]]; then
        IFS= read -rs val </dev/tty; echo
    else
        IFS= read -r val </dev/tty
    fi
    echo "${val:-$default}"
}

if [[ -f "$ENV_FILE" ]]; then
    warn ".env 已存在，跳過設定精靈（若要重設請先刪除 $ENV_FILE）"
else
    echo ""
    echo "  請依提示輸入設定（直接 Enter 保留預設值）"
    echo ""

    TGT=$(read_field "TARGET_TRADER_ADDRESS" \
        "🎯 目標交易員的錢包地址" \
        "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1")

    PRIVKEY=$(read_field "WALLET_PRIVATE_KEY" \
        "🔑 你的錢包私鑰（輸入不顯示）" "" "secret")

    ADDR=$(read_field "WALLET_ADDRESS" \
        "👛 你的錢包地址（可與上方私鑰對應地址相同）" "")

    CAPITAL=$(read_field "ALLOCATED_CAPITAL" \
        "💰 分配給跟單的 USDC 金額" "5000")

    DRAWDOWN=$(read_field "MAX_DRAWDOWN_PCT" \
        "📉 最大可接受虧損比例（0.20 = 20%）" "0.20")

    LIVE=$(read_field "LIVE_TRADING" \
        "⚡ 啟用真實下單？[true/false]（建議先 false 測試）" "false")

    INTERVAL=$(read_field "POLL_INTERVAL_SECONDS" \
        "⏱  輪詢間隔秒數（建議 5~10）" "5")

    MIN_NTL=$(read_field "MIN_ORDER_NOTIONAL" \
        "📏 最小下單名目值（USDC，低於此跳過）" "10")

    TG_TOKEN=$(read_field "TELEGRAM_BOT_TOKEN" \
        "📱 Telegram Bot Token（無則留空）" "")

    TG_CHAT=$(read_field "TELEGRAM_CHAT_ID" \
        "📱 Telegram Chat ID（無則留空）" "")

    NETWORK=$(read_field "NETWORK" \
        "🌐 網路 [mainnet/testnet]" "mainnet")

    cat > "$ENV_FILE" <<EOF
TARGET_TRADER_ADDRESS=$TGT
WALLET_PRIVATE_KEY=$PRIVKEY
WALLET_ADDRESS=$ADDR
ALLOCATED_CAPITAL=$CAPITAL
MAX_DRAWDOWN_PCT=$DRAWDOWN
LIVE_TRADING=$LIVE
POLL_INTERVAL_SECONDS=$INTERVAL
MIN_ORDER_NOTIONAL=$MIN_NTL
TELEGRAM_BOT_TOKEN=$TG_TOKEN
TELEGRAM_CHAT_ID=$TG_CHAT
NETWORK=$NETWORK
EOF
    chmod 600 "$ENV_FILE"
    info ".env 已建立（chmod 600）"
fi

# 基本驗證
PRIVKEY_VAL=$(grep -E "^WALLET_PRIVATE_KEY=" "$ENV_FILE" | cut -d= -f2- || true)
if [[ -z "$PRIVKEY_VAL" || "$PRIVKEY_VAL" == "your_private_key_here" ]]; then
    warn "⚠  WALLET_PRIVATE_KEY 未填入，請編輯 $ENV_FILE 後重新執行"
fi

LIVE_VAL=$(grep -E "^LIVE_TRADING=" "$ENV_FILE" | cut -d= -f2- || true)
if [[ "$LIVE_VAL" == "true" ]]; then
    warn "⚠  LIVE_TRADING=true，將執行真實下單！確認無誤後再繼續"
else
    info "LIVE_TRADING=false（乾跑模式，安全）"
fi

# ── 4. logs 目錄 ──────────────────────────────────────────
step "4/6  建立 logs 目錄"
mkdir -p "$PROJECT_DIR/logs"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT_DIR/logs" 2>/dev/null || \
    chown -R "$SERVICE_USER" "$PROJECT_DIR/logs" 2>/dev/null || true
info "logs/ 就緒"

# ── 5. systemd 服務 ───────────────────────────────────────
step "5/6  安裝 systemd 服務"

sed \
    -e "s|/home/ubuntu/projects/hl-copytrader|$PROJECT_DIR|g" \
    -e "s|User=ubuntu|User=$SERVICE_USER|g" \
    -e "s|Group=ubuntu|Group=$SERVICE_USER|g" \
    "$SERVICE_TPL" > "$SYSTEMD_DST"

systemctl daemon-reload
systemctl enable hl-copytrader
info "服務已啟用（開機自啟）"

# ── 6. 啟動 ──────────────────────────────────────────────
step "6/6  啟動服務"

if systemctl is-active --quiet hl-copytrader 2>/dev/null; then
    warn "服務已在運行，執行重啟..."
    systemctl restart hl-copytrader
else
    systemctl start hl-copytrader
fi

sleep 3
echo ""
systemctl status hl-copytrader --no-pager --lines=12 || true

# ── 完成 ─────────────────────────────────────────────────
echo ""
echo -e "${G}${BOLD}"
echo "╔══════════════════════════════════════════════════════╗"
echo "║                  安裝完成！                          ║"
echo "╚══════════════════════════════════════════════════════╝"
echo -e "${N}"
cat << 'TIPS'
  常用指令：
    journalctl -u hl-copytrader -f          # 即時查看 log
    systemctl status hl-copytrader           # 服務狀態
    systemctl stop    hl-copytrader          # 暫停跟單
    systemctl restart hl-copytrader          # 重啟
    sudo bash deploy/update.sh               # 更新程式碼
    python3 main.py --status                 # 查看目標交易員倉位
    python3 main.py --dry-run                # 手動跑乾跑模式

TIPS
