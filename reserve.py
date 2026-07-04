"""
手動保留 Hyperliquid 請求額度（reserveRequestWeight），用來打破「位址級請求額度」卡死。

背景：位址級額度 = 10000 + 累積成交量(USDC)。請求超額後 → 下不了單 → 沒新成交 →
額度不長 → 卡死。付 USDC 保留額度是官方的解套（1 weight ≈ 0.0005 USDC）。

用法：
  python reserve.py            # 只顯示目前額度狀態（唯讀，不扣款）
  python reserve.py 2000       # 保留 2000（約扣 1 USDC）；確認成功再加大
需要 .env 的 WALLET_ADDRESS（主帳戶）與 WALLET_PRIVATE_KEY（簽名/agent 私鑰）。
"""
import os
import sys

import requests
from dotenv import load_dotenv
from eth_account import Account
from hyperliquid.utils import constants
from hyperliquid.utils.signing import sign_l1_action, get_timestamp_ms
from hyperliquid.exchange import Exchange

load_dotenv()

account_address = os.getenv("WALLET_ADDRESS")
private_key = os.getenv("WALLET_PRIVATE_KEY")
if not account_address or not private_key:
    raise SystemExit("請檢查 .env：需要 WALLET_ADDRESS 與 WALLET_PRIVATE_KEY")

API = constants.MAINNET_API_URL


def rate_limit_status():
    r = requests.post(f"{API}/info",
                      json={"type": "userRateLimit", "user": account_address}, timeout=10)
    r.raise_for_status()
    return r.json()


# ── 1. 先看目前額度（唯讀，安全）──────────────────────────────
st = rate_limit_status()
used = int(st.get("nRequestsUsed", 0))
cap = int(st.get("nRequestsCap", 0))
vlm = float(st.get("cumVlm", 0) or 0)
print(f"主帳戶：{account_address}")
print(f"目前請求額度：已用 {used:,} / 上限 {cap:,}（累積成交量 ${vlm:,.2f}）")
over = used - cap
if over > 0:
    print(f"⚠️  已超額 {over:,} → 訂單被拒、卡死。"
          f"至少要保留 > {over:,}（建議多留緩衝，例如 {over + 5000:,}）才能解除。")
else:
    print(f"目前未超額（剩餘 {cap - used:,} 個請求）。")

# ── 2. 沒給參數就停在這（避免誤扣款）──────────────────────────
if len(sys.argv) < 2:
    print("\n只顯示狀態、未執行保留。要保留請執行：  python reserve.py <weight>")
    print("例如：python reserve.py 2000  （約扣 1 USDC，先小額確認機制可用）")
    raise SystemExit(0)

weight = int(sys.argv[1])

# ── 3. 簽名並送出 reserveRequestWeight（會扣款）────────────────
# 重要：reserveRequestWeight 是「帳戶級」動作，agent(API)錢包不能做
# （會被當成 agent 自己、回 "Must deposit"）。必須用「主帳戶」私鑰簽名。
from getpass import getpass

master_key = os.getenv("MASTER_PRIVATE_KEY") or getpass(
    "\n貼上『主帳戶』私鑰（0x...；輸入不顯示、不儲存）：").strip()
account = Account.from_key(master_key)
if account.address.lower() != account_address.lower():
    raise SystemExit(
        f"✗ 這把私鑰的地址是 {account.address}，與主帳戶 {account_address} 不符。\n"
        f"  reserveRequestWeight 必須用『主帳戶』(WALLET_ADDRESS) 的私鑰，不是 agent 私鑰。")
print(f"簽名地址(主帳戶)：{account.address}")
exchange = Exchange(account, API, account_address=account.address)

action = {"type": "reserveRequestWeight", "weight": weight}
ts = get_timestamp_ms()
signature = sign_l1_action(
    exchange.wallet,
    action,
    exchange.vault_address,
    ts,
    exchange.expires_after,
    exchange.base_url == constants.MAINNET_API_URL,
)
print(f"保留 {weight:,} 個請求額度（預估扣款 ≈ {weight * 0.0005:.4f} USDC）...")
result = exchange._post_action(action, signature, ts)
print("執行結果：", result)

# ── 4. 再查一次確認 ──────────────────────────────────────────
st2 = rate_limit_status()
print(f"保留後：已用 {int(st2.get('nRequestsUsed', 0)):,} / "
      f"上限 {int(st2.get('nRequestsCap', 0)):,}")
