"""
執行交易，包裝 Hyperliquid SDK 的 Exchange。
支援預設 perp DEX 與 xyz DEX（美股永續）。
"""
import logging
import math
import time
from typing import Optional

from . import telegram as tg

logger = logging.getLogger(__name__)

# 不支援跟單的標的（spot 現貨，無法透過 Exchange class 下單）
SKIP_COIN_PATTERNS = ["/"]   # 含 '/' 的是現貨交易對如 PURR/USDC


def _is_spot_coin(coin: str) -> bool:
    """判斷是否為現貨標的（不支援跟單）。"""
    return any(p in coin for p in SKIP_COIN_PATTERNS)


def _round_size(size: float, sz_decimals: int) -> float:
    factor = 10 ** sz_decimals
    return math.floor(size * factor) / factor


def _safe_coin_name(coin: str) -> str:
    """Telegram 訊息中安全顯示幣名（包在 code tag 內）。"""
    return f"<code>{coin}</code>"


def get_sz_decimals(info, coin: str) -> int:
    """
    從 meta 取得標的的 size decimals。
    支援 xyz: 前綴的美股標的（另外查詢 xyz DEX meta）。
    """
    try:
        if ":" in coin:
            dex, raw_coin = coin.split(":", 1)
            meta = info.meta(dex)
            for asset in meta.get("universe", []):
                if asset["name"] == raw_coin:
                    return asset["szDecimals"]
        else:
            meta = info.meta()
            for asset in meta["universe"]:
                if asset["name"] == coin:
                    return asset["szDecimals"]
    except Exception as e:
        logger.debug(f"get_sz_decimals({coin}) 失敗: {e}")
    return 4


class Trader:
    def __init__(self, exchange, info, live_trading: bool = False):
        self.exchange = exchange
        self.info = info
        self.live_trading = live_trading
        self._sz_dec: dict = {}

    def _get_sz_decimals(self, coin: str) -> int:
        if coin not in self._sz_dec:
            self._sz_dec[coin] = get_sz_decimals(self.info, coin)
        return self._sz_dec[coin]

    def _sdk_coin(self, coin: str) -> str:
        """傳給 SDK 的幣名。xyz:NVDA → 'xyz:NVDA'（SDK 用 : 判斷 DEX）。"""
        return coin

    def set_leverage(self, coin: str, leverage: int, is_cross: bool) -> bool:
        if not self.live_trading:
            logger.info(f"[DRY RUN] 設定 {coin} 槓桿 {leverage}x {'cross' if is_cross else 'isolated'}")
            return True
        try:
            result = self.exchange.update_leverage(leverage, self._sdk_coin(coin), is_cross)
            logger.info(f"設定 {coin} 槓桿 {leverage}x: {result}")
            return True
        except Exception as e:
            logger.error(f"設定 {coin} 槓桿失敗: {e}")
            tg.alert_error("槓桿設定失敗", f"{coin} {leverage}x: {e}")
            return False

    def open_position(self, coin: str, is_buy: bool, size: float,
                      leverage: int, is_cross: bool,
                      entry_px: float = 0, scale: float = 0,
                      trader_account: float = 0) -> Optional[dict]:
        # 現貨標的不支援跟單
        if _is_spot_coin(coin):
            logger.info(f"[SKIP] {coin} 是現貨標的，跳過跟單")
            return None

        sz_dec = self._get_sz_decimals(coin)
        size = _round_size(size, sz_dec)
        if size <= 0:
            logger.warning(f"[SKIP] {coin} 計算後 size={size}，跳過")
            return None

        side = "long" if is_buy else "short"
        notional = size * entry_px
        lev_type = "cross" if is_cross else "isolated"

        if not self.live_trading:
            logger.info(f"[DRY RUN] 開倉 {coin} {'多' if is_buy else '空'} size={size} lev={leverage}x")
            tg.notify_open(coin, side, size, entry_px, leverage, lev_type,
                           notional, scale, trader_account)
            return {"status": "dry_run"}

        self.set_leverage(coin, leverage, is_cross)
        try:
            result = self.exchange.market_open(self._sdk_coin(coin), is_buy, size)
            logger.info(f"開倉 {coin} {'多' if is_buy else '空'} size={size}: {result}")

            # 檢查回應中的錯誤
            statuses = (result or {}).get("response", {}).get("data", {}).get("statuses", [])
            for st in statuses:
                err = st.get("error", "")
                if not err:
                    continue
                err_lower = err.lower()
                if "insufficient" in err_lower or "margin" in err_lower:
                    tg.alert_insufficient_balance(0, notional / max(leverage, 1), coin)
                elif "market" in err_lower and "closed" in err_lower:
                    tg.alert_error("市場未開盤", f"{coin} 目前市場關閉（美股交易時段外）")
                else:
                    tg.alert_api_error(0, f"{coin} 開倉: {err}")
                return result

            tg.notify_open(coin, side, size, entry_px, leverage, lev_type,
                           notional, scale, trader_account)
            return result

        except Exception as e:
            err_str = str(e)
            logger.error(f"開倉 {coin} 失敗: {err_str}")
            err_lower = err_str.lower()
            if "key" in err_lower or "auth" in err_lower or "signature" in err_lower:
                tg.alert_api_error(-1, f"API Key 失效或未授權: {err_str}")
            elif "margin" in err_lower or "balance" in err_lower or "insufficient" in err_lower:
                tg.alert_insufficient_balance(0, notional / max(leverage, 1), coin)
            else:
                tg.alert_error("開倉失敗", f"{coin}: {err_str}")
            return None

    def close_position(self, coin: str, is_buy: bool, size: float,
                       unrealized_pnl: float = 0.0,
                       my_address: str = "",
                       api_url: str = "") -> Optional[dict]:
        """
        平倉。
        is_buy=True 表示我目前持有多單（賣出以平倉）。
        is_buy=False 表示我目前持有空單（買入以平倉）。
        """
        if _is_spot_coin(coin):
            logger.info(f"[SKIP] {coin} 是現貨標的，跳過")
            return None

        sz_dec = self._get_sz_decimals(coin)
        size = _round_size(size, sz_dec)
        if size <= 0:
            return None

        # is_buy=True 表示我持有多單 → 平倉時 Telegram 顯示 long
        side = "long" if is_buy else "short"
        action = "平多" if is_buy else "平空"

        if not self.live_trading:
            logger.info(f"[DRY RUN] 平倉 {coin} {action} size={size} pnl≈{unrealized_pnl:+.2f}")
            tg.notify_close(coin, side, size, unrealized_pnl)
            return {"status": "dry_run"}

        close_ts = int(time.time() * 1000)
        try:
            # SDK 的 market_close 自動從當前倉位判斷方向，只需傳正數 size
            result = self.exchange.market_close(self._sdk_coin(coin), size)
            logger.info(f"平倉 {coin} {action} size={size}: {result}")

            # 取得實際已實現盈虧
            realized_pnl = unrealized_pnl
            if my_address and api_url:
                from .monitor import get_recent_fills_pnl
                realized_pnl = get_recent_fills_pnl(api_url, my_address, coin, close_ts)
                if realized_pnl == 0.0:
                    realized_pnl = unrealized_pnl  # fallback

            tg.notify_close(coin, side, size, realized_pnl)
            return result

        except Exception as e:
            err_str = str(e)
            logger.error(f"平倉 {coin} 失敗: {err_str}")
            tg.alert_error("平倉失敗", f"{coin} {action}: {err_str}")
            return None

    def adjust_position(self, coin: str, current_size: float, target_size: float,
                        current_side: str, target_side: str,
                        leverage: int, is_cross: bool,
                        entry_px: float = 0, scale: float = 0,
                        trader_account: float = 0,
                        unrealized_pnl: float = 0.0,
                        my_address: str = "",
                        api_url: str = "") -> None:
        """
        調整倉位大小或方向。
        處理三種情況：
          1. 方向改變 → 全平再反向開倉
          2. 同向加倉 → 增加部位
          3. 同向減倉 → 部分平倉
        """
        if current_side != target_side:
            logger.info(f"{coin} 方向改變 {current_side}→{target_side}，先全平再開新倉")
            is_buy_to_close = current_side == "long"
            self.close_position(coin, is_buy_to_close, current_size,
                                unrealized_pnl, my_address, api_url)
            is_buy_to_open = target_side == "long"
            self.open_position(coin, is_buy_to_open, target_size, leverage, is_cross,
                               entry_px, scale, trader_account)
            return

        diff = target_size - current_size
        if abs(diff) < 1e-8:
            return

        if diff > 0:
            # 部分加倉
            is_buy = target_side == "long"
            logger.info(f"{coin} 加倉 +{diff:.4f}（{current_size:.4f}→{target_size:.4f}）")
            self.open_position(coin, is_buy, diff, leverage, is_cross,
                               entry_px, scale, trader_account)
        else:
            # 部分平倉
            reduce_size = abs(diff)
            is_buy_to_close = current_side == "long"
            partial_pnl = unrealized_pnl * (reduce_size / max(current_size, 1e-8))
            logger.info(f"{coin} 減倉 -{reduce_size:.4f}（{current_size:.4f}→{target_size:.4f}）")
            self.close_position(coin, is_buy_to_close, reduce_size,
                                partial_pnl, my_address, api_url)
