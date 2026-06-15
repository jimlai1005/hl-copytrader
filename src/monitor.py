"""
監控目標交易員的持倉狀態，透過 Hyperliquid REST API 輪詢。
支援預設 perp DEX 與 xyz DEX（美股永續）。
"""
import logging
import time
import requests
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 需要額外監控的 DEX 清單（xyz = 美股永續合約）
EXTRA_DEXS = ["xyz"]


def _post(api_url: str, payload: dict) -> Any:
    resp = requests.post(f"{api_url}/info", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _parse_positions(data: dict, dex_prefix: str = "") -> dict:
    """
    從 clearinghouseState 回傳值解析倉位。
    dex_prefix: 若非預設 DEX，在幣名前加上前綴（如 'xyz:'）。
    """
    positions = {}
    for item in data.get("assetPositions", []):
        pos = item["position"]
        size = float(pos["szi"])
        if size == 0:
            continue

        raw_coin = pos["coin"]
        coin = f"{dex_prefix}{raw_coin}" if dex_prefix else raw_coin

        lev_info = pos.get("leverage", {})
        positions[coin] = {
            "coin": coin,
            "raw_coin": raw_coin,       # 下單時傳給 SDK 的原始幣名
            "dex": dex_prefix.rstrip(":") if dex_prefix else "",
            "side": "long" if size > 0 else "short",
            "size": abs(size),
            "entry_px": float(pos.get("entryPx") or 0),
            "leverage": lev_info.get("value", 1),
            "leverage_type": lev_info.get("type", "cross"),
            "notional": float(pos.get("positionValue") or 0),
            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
        }
    return positions


def get_trader_state(api_url: str, address: str) -> dict:
    """
    回傳交易員的帳戶狀態，包含所有 DEX 的倉位。
    結構:
      {
        "account_value": float,
        "positions": { "BNB": {...}, "xyz:NVDA": {...}, ... }
      }
    """
    # 1. 預設 perp DEX
    data = _post(api_url, {"type": "clearinghouseState", "user": address})
    account_value = float(data["marginSummary"]["accountValue"])
    positions = _parse_positions(data)

    # 2. 額外 DEX（xyz 美股）
    for dex in EXTRA_DEXS:
        try:
            dex_data = _post(api_url, {
                "type": "clearinghouseState",
                "user": address,
                "dex": dex,
            })
            dex_account = float(dex_data["marginSummary"]["accountValue"])
            account_value += dex_account
            dex_positions = _parse_positions(dex_data, dex_prefix=f"{dex}:")
            positions.update(dex_positions)
        except Exception as e:
            logger.warning(f"查詢 {dex} DEX 倉位失敗: {e}")

    return {"account_value": account_value, "positions": positions}


def get_my_state(api_url: str, address: str) -> dict:
    """同 get_trader_state，用於查詢自己的帳戶。"""
    return get_trader_state(api_url, address)


def get_mid_price(api_url: str, coin: str) -> Optional[float]:
    """取得標的當前中間價。支援 xyz: 前綴。"""
    try:
        # xyz:NVDA → 查 xyz DEX 的 allMids
        if ":" in coin:
            dex, raw = coin.split(":", 1)
            data = _post(api_url, {"type": "allMids", "dex": dex})
            return float(data.get(raw, 0)) or None
        data = _post(api_url, {"type": "allMids"})
        return float(data.get(coin, 0)) or None
    except Exception as e:
        logger.warning(f"取得 {coin} 中間價失敗: {e}")
        return None


def get_recent_fills_pnl(api_url: str, address: str, coin: str,
                          since_ts: int) -> float:
    """
    取得指定幣種在 since_ts 之後的已實現盈虧合計。
    用於平倉後取得實際 P&L。
    """
    try:
        raw_coin = coin.split(":")[-1] if ":" in coin else coin
        fills = _post(api_url, {"type": "userFills", "user": address})
        total_pnl = 0.0
        for f in fills:
            if f["coin"] == raw_coin and f.get("time", 0) >= since_ts:
                closed = float(f.get("closedPnl", 0))
                fee = float(f.get("fee", 0))
                total_pnl += closed - fee
        return total_pnl
    except Exception as e:
        logger.warning(f"取得 {coin} 已實現盈虧失敗: {e}")
        return 0.0
