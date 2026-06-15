"""
執行交易，包裝 Hyperliquid SDK 的 Exchange。
所有下單前先驗證金額與槓桿，只使用市價單確保成交。
"""
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


def _round_size(size: float, sz_decimals: int) -> float:
    """依照 Hyperliquid 規定的小數位數進行四捨五入。"""
    factor = 10 ** sz_decimals
    return math.floor(size * factor) / factor


def get_sz_decimals(info, coin: str) -> int:
    """從 meta 取得標的的 size decimals。"""
    try:
        meta = info.meta()
        for asset in meta["universe"]:
            if asset["name"] == coin:
                return asset["szDecimals"]
    except Exception:
        pass
    return 4  # 預設值


class Trader:
    def __init__(self, exchange, info, live_trading: bool = False):
        self.exchange = exchange
        self.info = info
        self.live_trading = live_trading
        self._sz_decimals_cache: dict[str, int] = {}

    def _get_sz_decimals(self, coin: str) -> int:
        if coin not in self._sz_decimals_cache:
            self._sz_decimals_cache[coin] = get_sz_decimals(self.info, coin)
        return self._sz_decimals_cache[coin]

    def set_leverage(self, coin: str, leverage: int, is_cross: bool) -> bool:
        """設定槓桿。"""
        if not self.live_trading:
            logger.info(f"[DRY RUN] 設定 {coin} 槓桿 {leverage}x {'cross' if is_cross else 'isolated'}")
            return True
        try:
            result = self.exchange.update_leverage(leverage, coin, is_cross)
            logger.info(f"設定 {coin} 槓桿 {leverage}x: {result}")
            return True
        except Exception as e:
            logger.error(f"設定 {coin} 槓桿失敗: {e}")
            return False

    def open_position(self, coin: str, is_buy: bool, size: float,
                      leverage: int, is_cross: bool) -> Optional[dict]:
        """開倉：市價買入或賣出。"""
        sz_dec = self._get_sz_decimals(coin)
        size = _round_size(size, sz_dec)
        if size <= 0:
            logger.warning(f"[SKIP] {coin} 計算後 size={size}，跳過")
            return None

        side_str = "買入多單" if is_buy else "賣出空單"
        if not self.live_trading:
            logger.info(f"[DRY RUN] 開倉 {coin} {side_str} size={size} lev={leverage}x")
            return {"status": "dry_run"}

        self.set_leverage(coin, leverage, is_cross)
        try:
            result = self.exchange.market_open(coin, is_buy, size)
            logger.info(f"開倉 {coin} {side_str} size={size}: {result}")
            return result
        except Exception as e:
            logger.error(f"開倉 {coin} 失敗: {e}")
            return None

    def close_position(self, coin: str, is_buy: bool, size: float) -> Optional[dict]:
        """平倉：市價反向平。is_buy=True 表示平多(賣出)。"""
        sz_dec = self._get_sz_decimals(coin)
        size = _round_size(size, sz_dec)
        if size <= 0:
            return None

        action = "平多(賣)" if is_buy else "平空(買)"
        if not self.live_trading:
            logger.info(f"[DRY RUN] 平倉 {coin} {action} size={size}")
            return {"status": "dry_run"}

        try:
            result = self.exchange.market_close(coin, size if is_buy else -size)
            logger.info(f"平倉 {coin} {action} size={size}: {result}")
            return result
        except Exception as e:
            logger.error(f"平倉 {coin} 失敗: {e}")
            return None

    def adjust_position(self, coin: str, current_size: float, target_size: float,
                        current_side: str, target_side: str,
                        leverage: int, is_cross: bool) -> None:
        """調整倉位大小或方向。"""
        if current_side != target_side:
            logger.info(f"{coin} 方向改變 {current_side}→{target_side}，先全平再開新倉")
            is_buy_to_close = current_side == "long"
            self.close_position(coin, is_buy_to_close, current_size)
            is_buy_to_open = target_side == "long"
            self.open_position(coin, is_buy_to_open, target_size, leverage, is_cross)
            return

        diff = target_size - current_size
        if abs(diff) < 1e-8:
            return

        if diff > 0:
            is_buy = target_side == "long"
            logger.info(f"{coin} 加倉 {diff:.4f}")
            self.open_position(coin, is_buy, diff, leverage, is_cross)
        else:
            is_buy_close = target_side == "long"
            logger.info(f"{coin} 減倉 {abs(diff):.4f}")
            self.close_position(coin, is_buy_close, abs(diff))
