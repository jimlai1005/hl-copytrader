import os
from dotenv import load_dotenv

load_dotenv()


def _clean(val: str) -> str:
    """去掉行內註解與前後空白。
    為何需要：systemd 的 EnvironmentFile 不會去掉行內註解（python-dotenv 會），
    所以 `KEY=true   # 說明` 經 systemd 會變成 'true   # 說明'，直接 .lower()=='true' 會失敗。
    這裡統一處理，讓兩種載入方式行為一致。對「沒有行內註解」的值無任何影響。"""
    if val is None:
        return val
    return val.split("#", 1)[0].strip()


def _env_str(key: str, default: str) -> str:
    return _clean(os.getenv(key, default))


def _env_bool(key: str, default: str) -> bool:
    return _clean(os.getenv(key, default)).lower() == "true"


def _env_int(key: str, default: str) -> int:
    return int(_clean(os.getenv(key, default)))


def _env_float(key: str, default: str) -> float:
    return float(_clean(os.getenv(key, default)))


TARGET_TRADER = os.getenv("TARGET_TRADER_ADDRESS", "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1")
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
ALLOCATED_CAPITAL = _env_float("ALLOCATED_CAPITAL", "5000")

# 資金使用率：下單時只用 ALLOCATED_CAPITAL 的這個比例去縮放（1 = 不保留緩衝）。
# 預設 1.0 不做調整；若要保留保證金緩衝可調低（如 0.7）。
CAPITAL_UTILIZATION = _env_float("CAPITAL_UTILIZATION", "1.0")
if not 0 < CAPITAL_UTILIZATION <= 1:
    CAPITAL_UTILIZATION = 1.0

# 倉位權重 (0~1)：靜態手動權重，乘在每個跟單大小上。預設 1（不縮）。
# 最終權重 = POSITION_WEIGHT × 波動權重(若啟用)。
POSITION_WEIGHT = _env_float("POSITION_WEIGHT", "1.0")
if not 0 <= POSITION_WEIGHT <= 1:
    POSITION_WEIGHT = 1.0

# 波動權重：對目標「單日盈虧絕對值」算 14 天 Z-Score，市場妖度高(Z 大)時自動去槓桿。
# 權重 = 1 - clip(Z×0.2, 0, 0.7)，Z=3 時扣 60%、最多扣 70%(不會變負)。預設啟用。
VOLATILITY_WEIGHT_ENABLED = _env_bool("VOLATILITY_WEIGHT_ENABLED", "true")

# 波動權重曲線（weight 模組）：權重 = 1 - clip(Z×slope, 0, max_reduction)
VOL_LOOKBACK_DAYS = _env_int("VOL_LOOKBACK_DAYS", "14")
VOL_Z_SLOPE = _env_float("VOL_Z_SLOPE", "0.2")
VOL_Z_MAX_REDUCTION = _env_float("VOL_Z_MAX_REDUCTION", "0.7")

# 抗單保護：對目標「持倉時間」算 Z-Score，偵測他是否在硬撐抗單；異常時拒絕複製其補倉。
# 預設關閉（需額外抓成交歷史計算）。
HOLDING_PROTECTION_ENABLED = _env_bool("HOLDING_PROTECTION_ENABLED", "false")
# 持倉時間 Z-Score 超過此值即視為抗單（預設 2.0）。
HOLDING_PROTECTION_Z = _env_float("HOLDING_PROTECTION_Z", "2.0")

# 抗單保護（protection 模組）
HOLDING_LOOKBACK_DAYS = _env_int("HOLDING_LOOKBACK_DAYS", "14")
HOLDING_MIN_TRADES = _env_int("HOLDING_MIN_TRADES", "10")

MAX_DRAWDOWN_PCT = _env_float("MAX_DRAWDOWN_PCT", "0.20")
LIVE_TRADING = _env_bool("LIVE_TRADING", "false")
MIN_ORDER_NOTIONAL = _env_float("MIN_ORDER_NOTIONAL", "10")

# Telegram 通知開關。系統/警告/同步摘要一律發送；以下類別可自行開關：
NOTIFY_ORDERS = _env_bool("NOTIFY_ORDERS", "false")          # 掛單/改單明細
NOTIFY_OPENS = _env_bool("NOTIFY_OPENS", "false")            # 開倉明細
NOTIFY_VOLATILITY = _env_bool("NOTIFY_VOLATILITY", "true")   # 我的帳戶波動權重
NOTIFY_CLOSES = _env_bool("NOTIFY_CLOSES", "true")           # 平倉通知（含已實現盈虧）

# 目標有效槓桿上限保護（0=不啟用）：目標瀕臨清算時有效槓桿會暴衝，
# 設此值後，目標有效槓桿超過時自動把我方倉位等比例縮回此上限，避免被一起拖下水。
MAX_TARGET_LEVERAGE = _env_float("MAX_TARGET_LEVERAGE", "0")

# size 容忍度（0~1，預設 0.02=2%）：掛單/部位與目標的大小差距 <= 此比例就視為相同、不動，
# 避免本金或權益微幅波動造成無謂洗單。調高=更穩(少動)，調低=更貼目標(較常微調)。
SIZE_TOLERANCE = _env_float("SIZE_TOLERANCE", "0.02")
if not 0 <= SIZE_TOLERANCE < 1:
    SIZE_TOLERANCE = 0.02

# 每小時在第幾分鐘檢查並鏡像目標的掛單（0~59，預設 55，提早 5 分鐘掛單）
CHECK_MINUTE = _env_int("CHECK_MINUTE", "55")
if not 0 <= CHECK_MINUTE <= 59:
    CHECK_MINUTE = 55

# 美股「非活躍時段」的同步頻率（活躍時段一律每分鐘，不受此影響）：
#   "hourly"（預設）= 每小時第 CHECK_MINUTE 分同步一次
#   "5min"          = 每 5 分鐘同步一次
OFFHOURS_SYNC_MODE = _env_str("OFFHOURS_SYNC_MODE", "hourly").lower()
if OFFHOURS_SYNC_MODE not in ("hourly", "5min"):
    OFFHOURS_SYNC_MODE = "hourly"

# 是否跟單 xyz DEX（美股永續）。false = 只做 crypto（預設 perp DEX）。
ENABLE_XYZ = _env_bool("ENABLE_XYZ", "true")

# 進場掛單/部位的名目槓桿（cross）。"max"=用標的最大槓桿，最省保證金；或填數字指定倍率。
# 掛單佔用保證金 = 名目/槓桿，1x 會佔滿全額；倉位大小由跟單比例決定、與此無關，設高不增加風險。
ORDER_LEVERAGE = _env_str("ORDER_LEVERAGE", "max").lower()

# 計算跟單比例時，「目標本金」(分母) 是否含目標的 spot USDC。
# false（預設）= 只用目標 perp 權益(現金+未實現損益)，跟單比例反映目標真實 perp 槓桿。
# true = 含目標 spot，會在目標停大筆 USDC 在 spot 時讓我方倉位偏小。
# 註：此參數只影響「目標分母」；我方帳戶權益(回撤用)一律含 spot(unified account)。
TARGET_EQUITY_INCLUDE_SPOT = _env_bool("TARGET_EQUITY_INCLUDE_SPOT", "false")
NETWORK = _env_str("NETWORK", "mainnet")

HL_API_URL = "https://api.hyperliquid.xyz" if NETWORK == "mainnet" else "https://api.hyperliquid-testnet.xyz"
