"""
標的/下單相關的無狀態工具（從 trader.py 抽出，與 Trader 的執行狀態解耦）：
幣名與 DEX 判斷、現貨判斷、size 進位、meta 欄位查詢、order_type 組裝、下單錯誤路由。
"""
import logging
import math

from . import telegram as tg

logger = logging.getLogger(__name__)


def _is_spot_coin(coin: str) -> bool:
    """
    判斷是否為現貨標的（不支援跟單）。Hyperliquid 現貨有兩種表示法：
      - 交易對名稱含 '/'，如 PURR/USDC
      - 索引代號以 '@' 開頭，如 @85
    現貨不能設槓桿、size 規則不同，故一律跳過。
    """
    return coin.startswith("@") or "/" in coin


def _round_size(size: float, sz_decimals: int) -> float:
    factor = 10 ** sz_decimals
    return math.floor(size * factor) / factor


def _coin_dex(coin: str) -> str:
    """標的所屬 DEX：xyz:NVDA → 'xyz'；BTC → ''（預設 DEX）。"""
    return coin.split(":")[0] if ":" in coin else ""


def _order_type_and_px(spec: dict) -> tuple:
    """由 spec 算出下單價與 SDK order_type。回傳 (px, order_type)。"""
    if spec["is_trigger"]:
        px = spec["limit_px"] or spec["trigger_px"]
        order_type = {
            "trigger": {
                "triggerPx": spec["trigger_px"],
                "isMarket": spec["is_market"],
                "tpsl": spec["tpsl"] or "sl",
            }
        }
    else:
        px = spec["limit_px"]
        order_type = {"limit": {"tif": spec["tif"]}}
    return px, order_type


def _extract_order_error(result) -> str:
    """從下單/改單回應的 statuses 取出第一個錯誤訊息；無錯誤回空字串。"""
    statuses = (result or {}).get("response", {}).get("data", {}).get("statuses", [])
    for st in statuses:
        if isinstance(st, dict) and st.get("error"):
            return st["error"]
    return ""


def _route_order_error(coin: str, err: str, margin_required: float, context: str) -> None:
    """把下單/開倉回應的錯誤路由到對應的 Telegram 警告。"""
    el = err.lower()
    if "insufficient" in el or "margin" in el:
        tg.alert_insufficient_balance(0, margin_required, coin)
    elif "market" in el and "closed" in el:
        tg.alert_error("市場未開盤", f"{coin} 目前市場關閉（美股交易時段外）")
    else:
        tg.alert_api_error(0, f"{coin} {context}: {err}")


def _meta_field(info, coin: str, field: str, default):
    """從 meta universe 取標的的某欄位（用完整 coin 比對）。查不到回 default。"""
    try:
        dex = _coin_dex(coin)
        meta = info.meta(dex) if dex else info.meta()
        for asset in meta.get("universe", []):
            if asset["name"] == coin:
                return asset.get(field, default)
    except Exception as e:
        logger.debug(f"_meta_field({coin}, {field}) 失敗: {e}")
    return default


def get_sz_decimals(info, coin: str) -> int:
    return int(_meta_field(info, coin, "szDecimals", 4))


def get_max_leverage(info, coin: str) -> int:
    return int(_meta_field(info, coin, "maxLeverage", 0))


def get_only_isolated(info, coin: str) -> bool:
    return bool(_meta_field(info, coin, "onlyIsolated", False))
