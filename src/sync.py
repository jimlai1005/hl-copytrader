"""
核心同步邏輯：比較目標交易員的倉位與我的倉位，
計算需要執行的操作並呼叫 Trader 執行。
"""
import logging
from typing import Optional

from .config import ALLOCATED_CAPITAL, MIN_ORDER_NOTIONAL
from .monitor import get_mid_price
from .trader import Trader

logger = logging.getLogger(__name__)


def compute_scale_factor(trader_account_value: float) -> float:
    if trader_account_value <= 0:
        return 0.0
    return ALLOCATED_CAPITAL / trader_account_value


def sync_positions(
    api_url: str,
    trader: Trader,
    target_state: dict,
    my_state: dict,
    prev_target_positions: Optional[dict] = None,
) -> dict:
    """
    同步我的倉位與目標交易員的倉位。
    回傳包含 scale 與 actions 清單的 dict。
    """
    trader_account_value = target_state["account_value"]
    target_positions = target_state["positions"]
    my_positions = my_state["positions"]

    scale = compute_scale_factor(trader_account_value)
    logger.info(
        f"交易員帳戶 ${trader_account_value:,.0f} | "
        f"跟單資金 ${ALLOCATED_CAPITAL:,.0f} | "
        f"比例 {scale:.4f}"
    )

    actions = []

    # 1. 處理目標交易員有倉位的標的
    for coin, tgt_pos in target_positions.items():
        target_size = tgt_pos["size"] * scale
        target_side = tgt_pos["side"]
        leverage = tgt_pos["leverage"]
        is_cross = tgt_pos["leverage_type"] == "cross"
        mid_px = get_mid_price(api_url, coin) or tgt_pos["entry_px"]
        notional = target_size * mid_px

        if notional < MIN_ORDER_NOTIONAL:
            logger.debug(f"[SKIP] {coin} 目標倉位名目值 ${notional:.2f} 低於最小值 ${MIN_ORDER_NOTIONAL}")
            continue

        if coin not in my_positions:
            logger.info(f"[ACTION] 新開倉 {coin} {target_side} size={target_size:.4f} lev={leverage}x")
            is_buy = target_side == "long"
            result = trader.open_position(
                coin, is_buy, target_size, leverage, is_cross,
                entry_px=mid_px, scale=scale, trader_account=trader_account_value,
            )
            actions.append({"action": "open", "coin": coin, "side": target_side,
                            "size": target_size, "result": result})
        else:
            my_pos = my_positions[coin]
            my_size = my_pos["size"]
            my_side = my_pos["side"]
            size_diff_pct = abs(target_size - my_size) / max(my_size, 1e-8)

            if my_side != target_side or size_diff_pct > 0.02:
                logger.info(
                    f"[ACTION] 調整 {coin}: "
                    f"我的={my_side} {my_size:.4f} → 目標={target_side} {target_size:.4f}"
                )
                trader.adjust_position(
                    coin, my_size, target_size, my_side, target_side, leverage, is_cross,
                    entry_px=mid_px, scale=scale, trader_account=trader_account_value,
                    unrealized_pnl=my_pos.get("unrealized_pnl", 0),
                )
                actions.append({"action": "adjust", "coin": coin,
                                "from_size": my_size, "to_size": target_size})
            else:
                logger.debug(f"[OK] {coin} 倉位差距 {size_diff_pct:.1%}，無需調整")

    # 2. 我有倉位但目標交易員已平倉 → 跟著平
    for coin, my_pos in list(my_positions.items()):
        if coin not in target_positions:
            logger.info(f"[ACTION] 目標已平 {coin}，跟著平倉 size={my_pos['size']:.4f}")
            is_buy_close = my_pos["side"] == "long"
            result = trader.close_position(
                coin, is_buy_close, my_pos["size"],
                unrealized_pnl=my_pos.get("unrealized_pnl", 0),
            )
            actions.append({"action": "close", "coin": coin, "result": result})

    if not actions:
        logger.info("倉位已同步，無需操作")

    return {"scale": scale, "actions": actions}
