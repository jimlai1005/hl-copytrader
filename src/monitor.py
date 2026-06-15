"""
監控目標交易員的持倉狀態，透過 Hyperliquid REST API 輪詢。
"""
import logging
import requests
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _post(api_url: str, payload: dict) -> Any:
    resp = requests.post(f"{api_url}/info", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def get_trader_state(api_url: str, address: str) -> dict:
    """
    回傳交易員的帳戶狀態，包含所有倉位。
    結構:
      {
        "account_value": float,       # 總帳戶價值 USDC
        "positions": {
          "BNB": {
            "coin": "BNB",
            "side": "long" | "short",
            "size": float,            # 正數=多單 負數=空單
            "entry_px": float,
            "leverage": int,
            "leverage_type": "cross" | "isolated",
            "notional": float,        # 倉位市值
            "unrealized_pnl": float,
          },
          ...
        }
      }
    """
    data = _post(api_url, {"type": "clearinghouseState", "user": address})

    account_value = float(data["marginSummary"]["accountValue"])
    positions = {}

    for item in data.get("assetPositions", []):
        pos = item["position"]
        size = float(pos["szi"])
        if size == 0:
            continue

        coin = pos["coin"]
        lev_info = pos.get("leverage", {})
        positions[coin] = {
            "coin": coin,
            "side": "long" if size > 0 else "short",
            "size": abs(size),
            "entry_px": float(pos.get("entryPx") or 0),
            "leverage": lev_info.get("value", 1),
            "leverage_type": lev_info.get("type", "cross"),
            "notional": float(pos.get("positionValue") or 0),
            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
        }

    return {"account_value": account_value, "positions": positions}


def get_my_state(api_url: str, address: str) -> dict:
    """同 get_trader_state，用於查詢自己的帳戶。"""
    return get_trader_state(api_url, address)


def get_mid_price(api_url: str, coin: str) -> Optional[float]:
    """取得標的當前中間價。"""
    try:
        data = _post(api_url, {"type": "allMids"})
        return float(data.get(coin, 0)) or None
    except Exception as e:
        logger.warning(f"取得 {coin} 中間價失敗: {e}")
        return None
