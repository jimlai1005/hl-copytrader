"""
倉位權重模組（position sizing weight）+ 帳戶波動統計。

最終權重 = 手動權重 POSITION_WEIGHT × 波動權重（若啟用），介於 0~1，
乘在每個跟單大小上（掛單與安全網部位都會套用），等比例縮放曝險。

波動統計（偵測市場妖度，不看賺賠只看震盪）：
  - 取某帳戶過去 ~30 天每日盈虧，算「單日 |PnL|」序列。
  - 以最近 14 天 |PnL| 的平均 μ、標準差 σ 為基準，算今日 |PnL| 的 Z-Score。
  - 權重 = 1 - clip(Z×0.2, 0, 0.7)：Z 越高扣越多，Z=3 扣 60%，最多扣 70%（不會變負）。
跟單比例用「目標」的波動權重；另可對「我方」帳戶算同樣統計供監控（純顯示）。
資料抓取依地址快取，失敗時權重回 1（不縮，安全預設）。
"""
import logging
import time as _time
from collections import OrderedDict
from datetime import datetime, timezone
from statistics import mean, pstdev

from .config import POSITION_WEIGHT, VOLATILITY_WEIGHT_ENABLED, HL_API_URL, TARGET_TRADER, VOL_LOOKBACK_DAYS, VOL_Z_SLOPE, VOL_Z_MAX_REDUCTION
from .monitor import _post

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = VOL_LOOKBACK_DAYS
_Z_SLOPE = VOL_Z_SLOPE
_Z_MAX_REDUCTION = VOL_Z_MAX_REDUCTION
_CACHE_TTL = 300        # 波動統計快取秒數（日級訊號不需每分鐘重算）

_stats_cache = {}       # address -> {"ts": float, "stats": dict|None}


def compute_volatility_stats(address: str) -> dict:
    """
    計算某帳戶的波動統計。回傳 {today, mu, sigma, z, weight}；資料不足回 None。
    """
    abs_daily = _daily_abs_pnl(address)
    if len(abs_daily) < LOOKBACK_DAYS + 1:
        return None
    today = abs_daily[-1]
    baseline = abs_daily[-(LOOKBACK_DAYS + 1):-1]   # 前 14 天為基準
    mu = mean(baseline)
    sigma = pstdev(baseline)
    if sigma <= 0:
        return {"today": today, "mu": mu, "sigma": 0.0, "z": 0.0, "weight": 1.0}
    z = (today - mu) / sigma
    reduction = min(max(z * _Z_SLOPE, 0.0), _Z_MAX_REDUCTION)
    return {"today": today, "mu": mu, "sigma": sigma, "z": z, "weight": 1.0 - reduction}


def get_vol_stats(address: str) -> dict:
    """帶快取的波動統計（同地址 300 秒內重用）。失敗回 None。"""
    now = _time.time()
    c = _stats_cache.get(address)
    if c and now - c["ts"] < _CACHE_TTL:
        return c["stats"]
    try:
        stats = compute_volatility_stats(address)
    except Exception as e:
        logger.warning(f"計算波動統計失敗({address[:8]}…): {e}")
        stats = None
    _stats_cache[address] = {"ts": now, "stats": stats}
    return stats


def get_position_weight() -> float:
    """最終倉位權重（0~1）= 手動權重 × 目標的波動權重（若啟用）。每次同步呼叫。"""
    w = min(1.0, max(0.0, POSITION_WEIGHT))
    if VOLATILITY_WEIGHT_ENABLED:
        stats = get_vol_stats(TARGET_TRADER)
        if stats:
            w *= stats["weight"]
    return min(1.0, max(0.0, w))


def _daily_abs_pnl(address: str) -> list:
    """從 portfolio 的 month.pnlHistory(累積PnL) 推每日 |PnL|。"""
    pf = _post(HL_API_URL, {"type": "portfolio", "user": address})
    pnl_hist = None
    for row in pf:
        if isinstance(row, list) and len(row) == 2 and row[0] == "month":
            pnl_hist = row[1].get("pnlHistory", [])
            break
    if not pnl_hist:
        return []
    by_day = OrderedDict()
    for ts, val in pnl_hist:
        day = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).date()
        by_day[day] = float(val)
    cum = list(by_day.values())
    daily = [cum[i] - cum[i - 1] for i in range(1, len(cum))]
    return [abs(x) for x in daily]
