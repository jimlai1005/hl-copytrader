"""
清算冷卻模組。

從我方 userFillsByTime 找 `liquidation.liquidatedUser == 我方地址` 的成交（＝我被交易所清算），
該標的進入 LIQUIDATION_COOLDOWN_HOURS 冷卻：orders/sync 對它只准減倉/平倉、不新開不加倉、
不掛補倉單（沿用抗單保護的 protected 集合機制）。

為何：安全網沒有記憶，被清算後看到「目標有、我沒有」會 1 分鐘內市價重開，
在剛把我清掉的急拉後重進場，是第二次被清算機率最高的時點。

設計：
- 不需要狀態檔：冷卻表每次從成交紀錄重建（只看最近 COOLDOWN_HOURS），重啟後自然延續。
- 快取 60 秒；抓取失敗時沿用上次結果（安全預設是「維持保護」，不是「解除保護」）。
- 新出現的清算發一則 Telegram（alert_liquidated，帶 dedup key，一律發送）。
"""
import logging
import time as _time
from datetime import datetime

from .config import LIQUIDATION_COOLDOWN_HOURS
from .monitor import _post
from . import telegram as tg

logger = logging.getLogger(__name__)

COOLDOWN_HOURS = LIQUIDATION_COOLDOWN_HOURS
_CACHE_TTL = 60

# until: coin -> 冷卻截止 unix 秒；seen: 已告警的 (coin, 清算時間 ms)
_cache = {"ts": 0.0, "address": None, "until": {}}
_alerted = set()


def _my_liquidations(api_url: str, address: str, since_ms: int) -> list:
    """回傳 [(coin, time_ms, closed_pnl)]，只含「我方是被清算方」的成交。"""
    fills = _post(api_url, {"type": "userFillsByTime", "user": address, "startTime": since_ms})
    me = address.lower()
    out = []
    for f in fills:
        liq = f.get("liquidation")
        if not liq or str(liq.get("liquidatedUser", "")).lower() != me:
            continue
        out.append((f["coin"], int(f["time"]), float(f.get("closedPnl", 0) or 0)))
    return out


def get_liquidation_cooldowns(api_url: str, address: str, now: float = None) -> dict:
    """
    回傳 {coin: 冷卻截止 unix 秒}，只含仍在冷卻中的標的。
    COOLDOWN_HOURS <= 0 或 address 空 → {}。
    """
    if COOLDOWN_HOURS <= 0 or not address:
        return {}
    now = _time.time() if now is None else now
    if _cache["address"] == address and now - _cache["ts"] < _CACHE_TTL:
        return {c: u for c, u in _cache["until"].items() if u > now}

    window_s = COOLDOWN_HOURS * 3600
    try:
        events = _my_liquidations(api_url, address, int((now - window_s) * 1000))
    except Exception as e:
        logger.warning(f"取得清算紀錄失敗，沿用上次冷卻表: {e}")
        return {c: u for c, u in _cache["until"].items() if u > now}

    until = {}
    for coin, t_ms, pnl in events:
        end = t_ms / 1000 + window_s
        if end <= now:
            continue
        if end > until.get(coin, 0):
            until[coin] = end
        key = (coin, t_ms)
        if key not in _alerted:
            _alerted.add(key)
            until_str = datetime.fromtimestamp(end).strftime("%m/%d %H:%M")
            logger.warning(f"[清算冷卻] {coin} 於 {datetime.fromtimestamp(t_ms/1000):%m/%d %H:%M} 被清算"
                           f"（已實現 {pnl:+.2f}），冷卻至 {until_str}，期間只准減倉")
            tg.alert_liquidated(coin, pnl, until_str)

    _cache.update(ts=now, address=address, until=until)
    return dict(until)
