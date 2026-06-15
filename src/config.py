import os
from dotenv import load_dotenv

load_dotenv()

TARGET_TRADER = os.getenv("TARGET_TRADER_ADDRESS", "0xf97ad6704baec104d00b88e0c157e2b7b3a1ddd1")
WALLET_PRIVATE_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS", "")
ALLOCATED_CAPITAL = float(os.getenv("ALLOCATED_CAPITAL", "5000"))
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "0.20"))
LIVE_TRADING = os.getenv("LIVE_TRADING", "false").lower() == "true"
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "5"))
MIN_ORDER_NOTIONAL = float(os.getenv("MIN_ORDER_NOTIONAL", "10"))
NETWORK = os.getenv("NETWORK", "mainnet")

HL_API_URL = "https://api.hyperliquid.xyz" if NETWORK == "mainnet" else "https://api.hyperliquid-testnet.xyz"
