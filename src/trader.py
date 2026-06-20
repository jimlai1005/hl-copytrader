"""
執行交易，包裝 Hyperliquid SDK 的 Exchange。
支援預設 perp DEX 與 xyz DEX（美股永續）。
"""
import logging
import time
from typing import Optional

from . import telegram as tg
from .config import ORDER_LEVERAGE
from .resilience import ResilientExchange
from .instrument import (
    _is_spot_coin, _round_size, _coin_dex, _order_type_and_px,
    _extract_order_error, _route_order_error,
    get_sz_decimals, get_max_leverage, get_only_isolated,
)

logger = logging.getLogger(__name__)


def _position_exists(api_url: str, my_address: str, coin: str) -> bool:
    """驗證開倉是否已送達：偏向『假設已送達』。
    只有讀到部位、且確認該 coin『沒有部位』時才回 False（→ 允許重送）。
    無法查詢（缺位址 / 讀取失敗）一律回 True（假設已送達，絕不重複開倉）。"""
    if not (api_url and my_address):
        return True
    try:
        from .monitor import get_my_state
        return coin in get_my_state(api_url, my_address)["positions"]
    except Exception:
        return True


# 查不到標的最大槓桿時的後備倍率
ENTRY_LEVERAGE_FALLBACK = 20


class Trader:
    def __init__(self, exchange, info, live_trading: bool = False):
        self.exchange = ResilientExchange(exchange) if exchange is not None else None
        self.info = info
        self.live_trading = live_trading
        self._sz_dec: dict = {}
        self._max_lev: dict = {}
        self._only_iso: dict = {}

    def _get_sz_decimals(self, coin: str) -> int:
        if coin not in self._sz_dec:
            self._sz_dec[coin] = get_sz_decimals(self.info, coin)
        return self._sz_dec[coin]

    def _get_max_leverage(self, coin: str) -> int:
        if self.info is None:
            return 0
        if coin not in self._max_lev:
            self._max_lev[coin] = get_max_leverage(self.info, coin)
        return self._max_lev[coin]

    def entry_is_cross(self, coin: str) -> bool:
        """
        進場單/部位用 cross 還是 isolated。
        非預設 DEX（xyz 等 builder dex）或 onlyIsolated 資產 → isolated；其餘 → cross。
        （xyz 美股不支援 cross，設 cross 會報「Cross margin is not allowed」。）
        """
        if _coin_dex(coin):
            return False
        if self.info is None:
            return True
        if coin not in self._only_iso:
            self._only_iso[coin] = get_only_isolated(self.info, coin)
        return not self._only_iso[coin]

    def entry_leverage(self, coin: str) -> int:
        """
        進場單/部位要設定的名目槓桿（cross）。ORDER_LEVERAGE="max" 用標的最大槓桿，
        否則用指定數字（夾到上限）。掛單/部位佔用保證金 = 名目/槓桿，設高只省保證金、
        不影響倉位大小（大小由跟單比例決定），故不增加風險。
        """
        max_lev = self._get_max_leverage(coin)  # 0 = 未知（如 dry-run 無 info）
        if ORDER_LEVERAGE == "max":
            return max(1, max_lev if max_lev > 0 else ENTRY_LEVERAGE_FALLBACK)
        try:
            want = int(ORDER_LEVERAGE)
        except ValueError:
            want = ENTRY_LEVERAGE_FALLBACK
        return max(1, min(want, max_lev) if max_lev > 0 else want)

    def set_leverage(self, coin: str, leverage: int, is_cross: bool) -> bool:
        if _is_spot_coin(coin):
            return False  # 現貨無槓桿概念，避免「Spot not supported」錯誤
        if not self.live_trading:
            logger.info(f"[DRY RUN] 設定 {coin} 槓桿 {leverage}x {'cross' if is_cross else 'isolated'}")
            return True
        mode = "cross" if is_cross else "isolated"
        try:
            result = self.exchange.update_leverage(leverage, coin, is_cross)
            # 檢查 err 狀態（如「Cross margin is not allowed」不會丟例外、只回 err）
            if isinstance(result, dict) and result.get("status") == "err":
                err = result.get("response", "")
                logger.error(f"設定 {coin} 槓桿 {leverage}x {mode} 失敗: {err}")
                tg.alert_error("槓桿設定失敗", f"{coin} {leverage}x {mode}: {err}")
                return False
            logger.info(f"設定 {coin} 槓桿 {leverage}x {mode}: {result}")
            return True
        except Exception as e:
            logger.error(f"設定 {coin} 槓桿失敗: {e}")
            tg.alert_error("槓桿設定失敗", f"{coin} {leverage}x: {e}")
            return False

    def open_position(self, coin: str, is_buy: bool, size: float,
                      leverage: int, is_cross: bool,
                      entry_px: float = 0, scale: float = 0,
                      trader_account: float = 0,
                      my_address: str = "", api_url: str = "") -> Optional[dict]:
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
        verify = lambda: _position_exists(api_url, my_address, coin)
        try:
            if ":" in coin:
                # xyz DEX：SDK 的 _slippage_price 在沒有前綴時會查詢預設 DEX 取價，
                # 直接傳入已知的 mid price 繞過此缺陷。
                result = self.exchange.market_open(coin, is_buy, size, px=entry_px,
                                                   _verify=verify)
            else:
                result = self.exchange.market_open(coin, is_buy, size, _verify=verify)
            logger.info(f"開倉 {coin} {'多' if is_buy else '空'} size={size}: {result}")

            err = _extract_order_error(result)
            if err:
                _route_order_error(coin, err, notional / max(leverage, 1), "開倉")
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
            if ":" in coin:
                result = self._close_xyz(coin, is_buy, size, api_url)
                if result is None:
                    return None   # _close_xyz 已處理（取不到中間價並發警告）
            else:
                result = self.exchange.market_close(coin, size)

            logger.info(f"平倉 {coin} {action} size={size}: {result}")
            if result is None:
                logger.warning(f"平倉 {coin} 回傳 None，倉位可能已不存在，跳過通知")
                return None

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
                               entry_px=entry_px, scale=scale,
                               trader_account=trader_account,
                               my_address=my_address, api_url=api_url)
            return

        diff = target_size - current_size
        if abs(diff) < 1e-8:
            return

        if diff > 0:
            # 部分加倉
            is_buy = target_side == "long"
            logger.info(f"{coin} 加倉 +{diff:.4f}（{current_size:.4f}→{target_size:.4f}）")
            self.open_position(coin, is_buy, diff, leverage, is_cross,
                               entry_px=entry_px, scale=scale,
                               trader_account=trader_account,
                               my_address=my_address, api_url=api_url)
        else:
            # 部分平倉
            reduce_size = abs(diff)
            is_buy_to_close = current_side == "long"
            partial_pnl = unrealized_pnl * (reduce_size / max(current_size, 1e-8))
            logger.info(f"{coin} 減倉 -{reduce_size:.4f}（{current_size:.4f}→{target_size:.4f}）")
            self.close_position(coin, is_buy_to_close, reduce_size,
                                partial_pnl, my_address, api_url)

    def _close_xyz(self, coin: str, is_buy: bool, size: float, api_url: str):
        """xyz DEX 平倉：SDK market_close 找不到 xyz 倉位，改用 reduce-only IoC 限價單。
        回傳 SDK result；取不到中間價時發警告並回 None。"""
        from .monitor import get_mid_price
        dex_mid = get_mid_price(api_url, coin) if api_url else None
        if not dex_mid:
            logger.error(f"[xyz] 無法取得 {coin} 中間價，跳過平倉")
            tg.alert_error("平倉失敗", f"{coin} 無法取得中間價，請手動確認")
            return None
        close_is_buy = not is_buy  # 平多 → 賣 (False)；平空 → 買 (True)
        slippage = 0.05
        adj_px = dex_mid * (1 + slippage if close_is_buy else 1 - slippage)
        adj_px = float(f"{adj_px:.5g}")
        # reduce-only IoC → 包裝層判為冪等、直接重試（不會反向超平）
        return self.exchange.order(
            coin, close_is_buy, size, adj_px,
            order_type={"limit": {"tif": "Ioc"}},
            reduce_only=True,
        )

    # ── 掛單跟隨（open orders 鏡像）────────────────────────────
    def place_order(self, spec: dict) -> tuple:
        """
        依「已縮放好的掛單規格 spec」下單。支援限價單與觸發單（止盈/止損）。
        spec 由 orders.py 預先算好 size/price，欄位：
          coin, is_buy, size, limit_px, trigger_px, reduce_only,
          is_trigger, tpsl, is_market, tif, order_type_name
        回傳 (ok: bool, result)。ok=False 代表下單失敗或被拒（已發警告）。
        """
        coin = spec["coin"]
        if _is_spot_coin(coin):
            logger.info(f"[SKIP] {coin} 是現貨標的，跳過掛單")
            return (False, None)

        size = spec["size"]
        if size <= 0:
            return (False, None)

        is_buy = spec["is_buy"]
        reduce_only = spec["reduce_only"]
        px, order_type = _order_type_and_px(spec)

        if px <= 0:
            logger.warning(f"[SKIP] {coin} 掛單價格無效 ({px})，跳過")
            return (False, None)

        notional = size * px
        kind = spec["order_type_name"]
        ro_tag = " [reduceOnly]" if reduce_only else ""
        side_zh = "買" if is_buy else "賣"

        if not self.live_trading:
            logger.info(f"[DRY RUN] 掛單 {coin} {side_zh} size={size} @ ${px:,.4f} {kind}{ro_tag}")
            tg.notify_order_placed(coin, is_buy, size, px, kind, reduce_only, notional)
            return (True, {"status": "dry_run"})

        try:
            result = self.exchange.order(
                coin, is_buy, size, px,
                order_type=order_type, reduce_only=reduce_only,
            )
            logger.info(f"掛單 {coin} {side_zh} size={size} @ ${px:,.4f} {kind}{ro_tag}: {result}")

            err = _extract_order_error(result)
            if err:
                _route_order_error(coin, err, notional, "掛單")
                return (False, result)

            tg.notify_order_placed(coin, is_buy, size, px, kind, reduce_only, notional)
            return (True, result)

        except Exception as e:
            err_str = str(e)
            logger.error(f"掛單 {coin} 失敗: {err_str}")
            err_lower = err_str.lower()
            if "key" in err_lower or "auth" in err_lower or "signature" in err_lower:
                tg.alert_api_error(-1, f"API Key 失效或未授權: {err_str}")
            else:
                tg.alert_error("掛單失敗", f"{coin}: {err_str}")
            return (False, None)

    def modify_order(self, oid: int, spec: dict) -> bool:
        """
        就地修改既有掛單（改價/量）。成功回 True 並發 Telegram；
        失敗只回 False（不發警告，由呼叫端退回「取消舊單→重掛」處理）。
        """
        coin = spec["coin"]
        if _is_spot_coin(coin) or spec["size"] <= 0:
            return False
        is_buy = spec["is_buy"]
        reduce_only = spec["reduce_only"]
        size = spec["size"]
        px, order_type = _order_type_and_px(spec)
        if px <= 0:
            return False

        kind = spec["order_type_name"]
        side_zh = "買" if is_buy else "賣"
        if not self.live_trading:
            logger.info(f"[DRY RUN] 改單 {coin} oid={oid} → {side_zh} size={size} @ ${px:,.4f}")
            tg.notify_order_modified(coin, is_buy, size, px, kind, reduce_only)
            return True

        try:
            result = self.exchange.modify_order(
                oid, coin, is_buy, size, px, order_type, reduce_only=reduce_only,
            )
            err = _extract_order_error(result)
            if err:
                logger.warning(f"改單 {coin} oid={oid} 失敗（將退回取消重掛）: {err}")
                return False
            logger.info(f"改單 {coin} oid={oid} → {side_zh} size={size} @ ${px:,.4f}: ok")
            tg.notify_order_modified(coin, is_buy, size, px, kind, reduce_only)
            return True
        except Exception as e:
            logger.warning(f"改單 {coin} oid={oid} 例外（將退回取消重掛）: {e}")
            return False

    def cancel_one(self, coin: str, oid: int) -> bool:
        """取消單一掛單，回傳是否成功。"""
        if not self.live_trading:
            logger.info(f"[DRY RUN] 取消掛單 {coin} oid={oid}")
            return True
        try:
            self.exchange.cancel(coin, oid)
            logger.info(f"取消掛單 {coin} oid={oid}")
            return True
        except Exception as e:
            logger.error(f"取消 {coin} oid={oid} 失敗: {e}")
            return False
