"""
抗單保護模組（偵測目標是否在硬撐抗單）。

頂級毒瘤交易員：平時勝率高（小賺就跑），遇黑天鵝時死不認賠、瘋狂抗單甚至逆勢補倉。
作法：對目標「每筆交易的持倉時間」算 Z-Score——
  - 從過去 14 天成交記錄重建每筆交易(開倉→平倉)的持倉時間，算平均 μ、標準差 σ。
  - 對目前每個未平倉部位，算「目前持倉時間」的 Z-Score。
  - Z > HOLDING_PROTECTION_Z（預設 2.0）視為抗單 → 該標的拒絕複製新補倉單。

預設關閉（HOLDING_PROTECTION_ENABLED=false）；關閉時完全不做這段計算。
資料抓取有快取，失敗時回傳空（不啟動保護，安全預設）。
"""
import logging
import time as _time
from collections import defaultdict
from statistics import mean, pstdev

from .config import HL_API_URL, HOLDING_PROTECTION_Z, HOLDING_LOOKBACK_DAYS, HOLDING_MIN_TRADES
from .monitor import _post
from .trader import _is_spot_coin

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = HOLDING_LOOKBACK_DAYS
MIN_TRADES = HOLDING_MIN_TRADES
_CACHE_TTL = 300

_cache = {"ts": 0.0, "address": None, "flags": {}}


def get_anti_holding_flags(address: str, current_positions: dict) -> dict:
    """
    回傳被判定為「抗單」的標的 {coin: z_score}。current_positions 為目標目前持倉。
    有快取（同地址 300 秒內重用）。
    """
    now = _time.time()
    if (_cache["address"] == address and now - _cache["ts"] < _CACHE_TTL):
        return _cache["flags"]
    flags = _compute_flags(address, current_positions)
    _cache.update(ts=now, address=address, flags=flags)
    return flags


def _compute_flags(address: str, current_positions: dict) -> dict:
    try:
        completed, open_since = _holding_stats(address)
        if len(completed) < MIN_TRADES:
            logger.info(f"抗單保護：完成交易數不足({len(completed)})，暫不啟動")
            return {}
        mu = mean(completed)
        sigma = pstdev(completed)
        if sigma <= 0:
            return {}
        now = _time.time()
        flags = {}
        for coin in current_positions:
            opened = open_since.get(coin)
            if opened is None:
                continue
            holding = now - opened
            z = (holding - mu) / sigma
            if z > HOLDING_PROTECTION_Z:
                flags[coin] = z
                logger.warning(
                    f"抗單偵測：{coin} 已持倉 {holding/3600:.1f}h "
                    f"(平均 {mu/3600:.1f}h, Z={z:.1f}) → 拒絕補倉"
                )
        return flags
    except Exception as e:
        logger.warning(f"抗單保護計算失敗，暫不啟動: {e}")
        return {}


def _holding_stats(address: str) -> tuple:
    """
    重建每筆交易持倉時間。回傳 (completed_seconds[list], open_since{coin: 開倉unix秒})。
    用 startPosition + 帶號 sz 追蹤部位：0→非0 為開倉、非0→0 為平倉。
    """
    since = int((_time.time() - LOOKBACK_DAYS * 86400) * 1000)
    fills = _post(HL_API_URL, {
        "type": "userFillsByTime", "user": address, "startTime": since,
    })
    by_coin = defaultdict(list)
    for f in fills:
        coin = f.get("coin", "")
        if not coin or _is_spot_coin(coin):
            continue
        by_coin[coin].append(f)

    completed = []
    open_since = {}
    for coin, fs in by_coin.items():
        fs.sort(key=lambda x: x.get("time", 0))
        cur_open = None
        for f in fs:
            start = float(f.get("startPosition") or 0)
            sz = float(f.get("sz") or 0)
            delta = sz if f.get("side") == "B" else -sz
            after = start + delta
            t = f.get("time", 0) / 1000
            if abs(start) < 1e-9 and abs(after) > 1e-9:
                cur_open = t                      # 全新開倉
            elif abs(after) < 1e-9 and cur_open is not None:
                completed.append(t - cur_open)    # 平回 0 → 一筆完成
                cur_open = None
        if cur_open is not None:
            open_since[coin] = cur_open           # 目前仍持倉
    return completed, open_since
