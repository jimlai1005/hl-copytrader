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

# 跨發行版安裝套件（Ubuntu/Debian=apt、Amazon Linux/RHEL=dnf/yum）
pkg_install() {
    if command -v apt-get &>/dev/null; then
        apt-get update -y -qq && apt-get install -y "$@"
    elif command -v dnf &>/dev/null; then
        dnf install -y "$@"
    elif command -v yum &>/dev/null; then
        yum install -y "$@"
    else
        warn "無法判斷套件管理器，請手動安裝：$*"
        return 1
    fi
}

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
if [[ -z "$PYTHON_BIN" ]]; then
    warn "找不到 python3，嘗試自動安裝..."
    pkg_install python3 python3-pip || { err "請手動安裝 python3 python3-pip 後重跑"; exit 1; }
    PYTHON_BIN=$(command -v python3)
fi

PYVER=$("$PYTHON_BIN" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PYMAJ=$(echo "$PYVER" | cut -d. -f1)
PYMIN=$(echo "$PYVER" | cut -d. -f2)
{ [[ "$PYMAJ" -ge 3 ]] && [[ "$PYMIN" -ge 8 ]]; } || {
    err "需要 Python 3.8+，目前版本：$PYVER"
    exit 1
}
info "Python $PYVER"

# 確保能「實際建立」venv。注意：Ubuntu 上 `python3 -m venv --help` 可動，
# 但建立時 ensurepip 需要版本專屬套件（如 python3.12-venv），故這裡真的試建一次。
_venv_ok() {
    local probe; probe="$(mktemp -d)/probe"
    "$PYTHON_BIN" -m venv "$probe" &>/dev/null; local rc=$?
    rm -rf "$probe"
    return $rc
}
if ! _venv_ok; then
    warn "無法建立 venv（缺 ensurepip 套件），嘗試自動安裝..."
    if command -v apt-get &>/dev/null; then
        pkg_install "python${PYVER}-venv" python3-venv python3-pip
    else
        pkg_install python3-pip
    fi
fi
if ! _venv_ok; then
    err "無法建立 Python 虛擬環境，請手動安裝：sudo apt-get install -y python${PYVER}-venv"
    exit 1
fi
info "venv 可正常建立"

# ── 2. Python venv ────────────────────────────────────────
step "2/6  建立 Python 虛擬環境"

# 若上次失敗留下不完整的 venv（沒有可用的 python3），先移除重建
if [[ -d "$VENV_DIR" && ! -x "$VENV_DIR/bin/python3" ]]; then
    warn "偵測到不完整的 venv，移除重建..."
    rm -rf "$VENV_DIR"
fi

if [[ ! -d "$VENV_DIR" ]]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR" || { err "建立 venv 失敗"; rm -rf "$VENV_DIR"; exit 1; }
    info "已建立 venv"
else
    info "venv 已存在，僅更新套件"
fi

"$VENV_DIR/bin/pip" install --upgrade pip --quiet
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt" --quiet
info "Python 套件安裝完成"

# ── 3. 設定 .env ──────────────────────────────────────────
step "3/6  設定 .env"

# 讀取單一欄位：read_field "說明" "預設值" [secret]
# 提示一律輸出到 stderr，只有「值」走 stdout，才不會被 $(...) 連提示一起抓進去。
read_field() {
    local desc="$1" default="$2" secret="${3:-}"
    ask "$desc" >&2
    if [[ -n "$default" ]]; then
        printf "   (預設: %s) > " "$default" >&2
    else
        printf "   > " >&2
    fi
    local val
    if [[ "$secret" == "secret" ]]; then
        IFS= read -rs val </dev/tty; echo >&2
    else
        IFS= read -r val </dev/tty
    fi
    printf '%s' "${val:-$default}"
}

# 就地改寫 .env 的一行：set_env KEY VALUE（用 | 當 sed 分隔符，私鑰/地址/token 皆不含 |）
set_env() {
    local key="$1" val="$2"
    local esc; esc=$(printf '%s' "$val" | sed -e 's/[&|]/\\&/g')
    if grep -qE "^${key}=" "$ENV_FILE"; then
        sed -i "s|^${key}=.*|${key}=${esc}|" "$ENV_FILE"
    else
        echo "${key}=${val}" >> "$ENV_FILE"
    fi
}

if [[ -f "$ENV_FILE" ]]; then
    warn ".env 已存在，跳過設定精靈（若要重設請先刪除 $ENV_FILE）"
else
    # 以 .env.example 為基底（含最新參數與完整註解），精靈只覆寫關鍵欄位，
    # 其餘進階參數保留 .env.example 預設值，避免 heredoc 與設定漂移。
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    echo ""
    echo "  以 .env.example 為基底，請填入關鍵欄位（直接 Enter 保留預設 / 留空）"
    echo "  其餘進階參數已採用預設值，之後可自行編輯 $ENV_FILE"
    echo ""

    TGT=$(read_field "🎯 目標交易員的『主帳戶』地址" \
        "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1")
    set_env TARGET_TRADER_ADDRESS "$TGT"

    PRIVKEY=$(read_field "🔑 你的『簽名用』私鑰（agent 或主錢包皆可，輸入不顯示）" "" "secret")
    [[ -n "$PRIVKEY" ]] && set_env WALLET_PRIVATE_KEY "$PRIVKEY"

    ADDR=$(read_field "👛 你的『主帳戶』地址＝實際持有 USDC 的那個（切勿填 agent 地址！若上面用主錢包私鑰則填同一個）" "")
    [[ -n "$ADDR" ]] && set_env WALLET_ADDRESS "$ADDR"

    CAPITAL=$(read_field "💰 分配給跟單的 USDC 金額（<=0 = 自動用帳戶當前權益）" "5000")
    set_env ALLOCATED_CAPITAL "$CAPITAL"

    DRAWDOWN=$(read_field "📉 最大可接受回撤比例（0.20 = 20%）" "0.20")
    set_env MAX_DRAWDOWN_PCT "$DRAWDOWN"

    LIVE=$(read_field "⚡ 啟用真實下單？[true/false]（建議先 false 觀察）" "false")
    set_env LIVE_TRADING "$LIVE"

    TG_TOKEN=$(read_field "📱 Telegram Bot Token（無則留空）" "")
    [[ -n "$TG_TOKEN" ]] && set_env TELEGRAM_BOT_TOKEN "$TG_TOKEN"

    TG_CHAT=$(read_field "📱 Telegram Chat ID（無則留空）" "")
    [[ -n "$TG_CHAT" ]] && set_env TELEGRAM_CHAT_ID "$TG_CHAT"

    NETWORK=$(read_field "🌐 網路 [mainnet/testnet]" "mainnet")
    set_env NETWORK "$NETWORK"

    info ".env 已建立（chmod 600，基於 .env.example）"
fi

# 基本驗證
PRIVKEY_VAL=$(grep -E "^WALLET_PRIVATE_KEY=" "$ENV_FILE" | cut -d= -f2- || true)
if [[ -z "$PRIVKEY_VAL" || "$PRIVKEY_VAL" == your_*private_key* ]]; then
    warn "⚠  WALLET_PRIVATE_KEY 尚未填入，請編輯 $ENV_FILE 後再 sudo bash deploy/setup.sh"
fi

ADDR_VAL=$(grep -E "^WALLET_ADDRESS=" "$ENV_FILE" | cut -d= -f2- || true)
if [[ -z "$ADDR_VAL" || "$ADDR_VAL" == your_*account_address* ]]; then
    warn "⚠  WALLET_ADDRESS 尚未填入（要填主帳戶地址），請編輯 $ENV_FILE 後再重跑"
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
    .venv/bin/python main.py --status        # 查看目標交易員倉位
    .venv/bin/python main.py --dry-run       # 手動跑乾跑模式

TIPS
