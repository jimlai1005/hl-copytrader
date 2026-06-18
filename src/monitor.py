"""
監控目標交易員的持倉狀態，透過 Hyperliquid REST API 輪詢。
支援預設 perp DEX 與 xyz DEX（美股永續）。
"""
import logging
import requests
from typing import Any, Optional

from .config import ENABLE_XYZ

logger = logging.getLogger(__name__)

# 需要額外監控的 DEX 清單（xyz = 美股永續合約）；ENABLE_XYZ=false 時清空，只做 crypto。
EXTRA_DEXS = ["xyz"] if ENABLE_XYZ else []


def _post(api_url: str, payload: dict) -> Any:
    resp = requests.post(f"{api_url}/info", json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _canon_coin(raw_coin: str, dex: str) -> str:
    """
    正規化成標準幣名（meta/allMids/SDK 都用這個）。
    非預設 DEX（如 xyz）的標的本身已含前綴（如 'xyz:NVDA'）；
    若 API 偶爾回未含前綴的名稱，這裡補上，確保恰好一層前綴。
    """
    if dex and not raw_coin.startswith(f"{dex}:"):
        return f"{dex}:{raw_coin}"
    return raw_coin


def _parse_positions(data: dict, dex: str = "") -> dict:
    """從 clearinghouseState 回傳值解析倉位。dex 為非預設 DEX 名（如 'xyz'）。"""
    positions = {}
    for item in data.get("assetPositions", []):
        pos = item["position"]
        size = float(pos["szi"])
        if size == 0:
            continue

        coin = _canon_coin(pos["coin"], dex)
        lev_info = pos.get("leverage", {})
        positions[coin] = {
            "coin": coin,
            "dex": dex,
            "side": "long" if size > 0 else "short",
            "size": abs(size),
            "entry_px": float(pos.get("entryPx") or 0),
            "leverage": lev_info.get("value", 1),
            "leverage_type": lev_info.get("type", "cross"),
            "notional": float(pos.get("positionValue") or 0),
            "unrealized_pnl": float(pos.get("unrealizedPnl") or 0),
        }
    return positions


def _spot_usdc(api_url: str, address: str) -> float:
    """取得 spot 錢包的 USDC 餘額。unified account 下 spot 也是可用抵押品。"""
    try:
        spot = _post(api_url, {"type": "spotClearinghouseState", "user": address})
        total = 0.0
        for b in spot.get("balances", []):
            if b.get("coin") == "USDC":
                total += float(b.get("total") or 0)
        return total
    except Exception as e:
        logger.warning(f"查詢 spot 餘額失敗: {e}")
        return 0.0


def get_trader_state(api_url: str, address: str, include_spot: bool = False) -> dict:
    """
    回傳交易員的帳戶狀態，包含所有 DEX 的倉位。
    結構:
      {
        "account_value": float,   # 帳戶權益 equity
        "positions": { "BNB": {...}, "xyz:NVDA": {...}, ... }
      }

    account_value = Σ(各 perp DEX 的 marginSummary.accountValue) (+ include_spot 時加 spot USDC)。
    用 accountValue 加總是 Hyperliquid 的權威權益值：unified account 下 spot 抵押品已折入
    perp.accountValue（且 totalRawUsd 可能為負＝向 spot 借款），故不可用 raw+uPnL（會算成負值）。
    對 unified 與一般帳戶都正確；一般帳戶各 DEX accountValue 即各自 perp 權益。

    include_spot=True 時額外加 spot 錢包 USDC（僅在 spot 未折入 accountValue 的非 unified 情境需要）。
    """
    # 1. 預設 perp DEX：accountValue 與倉位
    data = _post(api_url, {"type": "clearinghouseState", "user": address})
    account_value = float(data["marginSummary"].get("accountValue") or 0)
    positions = _parse_positions(data)

    # 2. 額外 DEX（xyz 美股）：accountValue 加總
    for dex in EXTRA_DEXS:
        try:
            dex_data = _post(api_url, {
                "type": "clearinghouseState",
                "user": address,
                "dex": dex,
            })
            account_value += float(dex_data["marginSummary"].get("accountValue") or 0)
            positions.update(_parse_positions(dex_data, dex=dex))
        except Exception as e:
            logger.warning(f"查詢 {dex} DEX 倉位失敗: {e}")

    if include_spot:
        account_value += _spot_usdc(api_url, address)

    return {
        "account_value": account_value,
        "positions": positions,
    }


def get_my_state(api_url: str, address: str) -> dict:
    """查詢自己的帳戶。account_value = Σ各 DEX accountValue（unified 下已含 spot 抵押品）。"""
    return get_trader_state(api_url, address, include_spot=False)


def get_recent_peak_equity(api_url: str, address: str) -> float:
    """
    取得近期帳戶權益高點（用於回撤基準）。
    從 portfolio 的 week.accountValueHistory 取最大值（涵蓋約一週、含昨日高點）。
    取不到回 0。
    """
    try:
        pf = _post(api_url, {"type": "portfolio", "user": address})
        peak = 0.0
        for row in pf:
            if isinstance(row, list) and len(row) == 2 and row[0] == "week":
                for _ts, val in row[1].get("accountValueHistory", []):
                    peak = max(peak, float(val))
        return peak
    except Exception as e:
        logger.warning(f"取得近期權益高點失敗: {e}")
        return 0.0


def _parse_orders(orders_raw: list, dex: str = "") -> list:
    """
    從 frontendOpenOrders 回傳值解析掛單。dex 為非預設 DEX 名（如 'xyz'）。
    side: "B"=bid=買, "A"=ask=賣。支援限價單與觸發單（止盈/止損）。
    """
    result = []
    for o in orders_raw:
        coin = _canon_coin(o["coin"], dex)

        is_trigger = bool(o.get("isTrigger", False))
        order_type_name = o.get("orderType", "Limit")
        tpsl = None
        is_market = False
        if is_trigger:
            is_market = "Market" in order_type_name
            low = order_type_name.lower()
            if "take profit" in low or low.startswith("tp"):
                tpsl = "tp"
            elif "stop" in low or "sl" in low:
                tpsl = "sl"

        result.append({
            "coin": coin,
            "dex": dex,
            "oid": o["oid"],
            "is_buy": o["side"] == "B",
            "limit_px": float(o.get("limitPx") or 0),
            "size": abs(float(o.get("sz") or 0)),
            "reduce_only": bool(o.get("reduceOnly", False)),
            "is_trigger": is_trigger,
            "trigger_px": float(o.get("triggerPx") or 0),
            "tpsl": tpsl,
            "is_market": is_market,
            "tif": o.get("tif") or "Gtc",
            "order_type_name": order_type_name,
        })
    return result


def get_trader_open_orders(api_url: str, address: str) -> list:
    """
    取得交易員所有未成交掛單（含 xyz DEX）。
    回傳 list，每筆為 _parse_orders 的正規化結構。
    """
    orders = []
    try:
        data = _post(api_url, {"type": "frontendOpenOrders", "user": address})
        orders.extend(_parse_orders(data))
    except Exception as e:
        logger.warning(f"查詢預設 DEX 掛單失敗: {e}")

    for dex in EXTRA_DEXS:
        try:
            dex_data = _post(api_url, {
                "type": "frontendOpenOrders",
                "user": address,
                "dex": dex,
            })
            orders.extend(_parse_orders(dex_data, dex=dex))
        except Exception as e:
            logger.warning(f"查詢 {dex} DEX 掛單失敗: {e}")

    return orders


def get_my_open_orders(api_url: str, address: str) -> list:
    """同 get_trader_open_orders，用於查詢自己的掛單。"""
    return get_trader_open_orders(api_url, address)


def get_mid_price(api_url: str, coin: str) -> Optional[float]:
    """取得標的當前中間價。allMids 的 key 為標準幣名（xyz 為 'xyz:NVDA'）。"""
    try:
        # xyz:NVDA → 查 xyz DEX 的 allMids，並用完整幣名當 key
        dex = coin.split(":")[0] if ":" in coin else ""
        data = _post(api_url, {"type": "allMids", "dex": dex}) if dex \
            else _post(api_url, {"type": "allMids"})
        return float(data.get(coin, 0)) or None
    except Exception as e:
        logger.warning(f"取得 {coin} 中間價失敗: {e}")
        return None


def get_recent_fills_pnl(api_url: str, address: str, coin: str,
                          since_ts: int) -> float:
    """
    取得指定幣種在 since_ts 之後的已實現盈虧合計。
    用於平倉後取得實際 P&L。
    使用 userFillsByTime 以避免拉全量歷史成交記錄。
    """
    try:
        # 成交記錄的 coin 可能是完整名(xyz:NVDA)或去前綴名(NVDA)，兩者都比對
        raw_coin = coin.split(":")[-1] if ":" in coin else coin
        fills = _post(api_url, {
            "type": "userFillsByTime",
            "user": address,
            "startTime": since_ts,
        })
        total_pnl = 0.0
        for f in fills:
            if f["coin"] == coin or f["coin"] == raw_coin:
                closed = float(f.get("closedPnl", 0))
                fee = float(f.get("fee", 0))
                total_pnl += closed - fee
        return total_pnl
    except Exception as e:
        logger.warning(f"取得 {coin} 已實現盈虧失敗: {e}")
        return 0.0
