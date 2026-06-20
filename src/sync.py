"""
核心同步邏輯：比較目標交易員的倉位與我的倉位，
計算需要執行的操作並呼叫 Trader 執行。
支援預設 perp DEX 與 xyz DEX（美股永續）。
"""
import logging
from typing import Optional

from .config import (
    ALLOCATED_CAPITAL, CAPITAL_UTILIZATION, MIN_ORDER_NOTIONAL, SIZE_TOLERANCE,
    MAX_TARGET_LEVERAGE,
)
from .monitor import get_mid_price
from .trader import Trader
from .instrument import _coin_dex
from .weight import get_position_weight

logger = logging.getLogger(__name__)


def resolve_capital(my_equity: float) -> float:
    """
    跟單本金：ALLOCATED_CAPITAL > 0 用設定值；<= 0 則自動用我方帳戶當前權益
    （my_equity，含 spot 的 unified 淨值，有多少用多少）。
    """
    return ALLOCATED_CAPITAL if ALLOCATED_CAPITAL > 0 else max(my_equity, 0.0)


def compute_scale_factor(trader_equity: float, my_equity: float,
                         target_notional: float = 0.0) -> float:
    """
    跟單比例 = (跟單本金 × 資金使用率 × 倉位權重) / 對方帳戶淨值。
    本金見 resolve_capital（可設定固定值或自動抓帳戶權益）。
    分母用「帳戶淨值 equity（含未實現損益）」，使我方保證金使用率自動等於目標。
    倉位權重(0~1)由 weight 模組提供。
    MAX_TARGET_LEVERAGE>0 時，若目標有效槓桿(部位名目/淨值)超過上限，
    等比例縮回——保護瀕臨清算時目標槓桿暴衝把我方拖下水。
    """
    cap = resolve_capital(my_equity)
    if trader_equity <= 0 or cap <= 0:
        return 0.0
    scale = (cap * CAPITAL_UTILIZATION * get_position_weight()) / trader_equity
    if MAX_TARGET_LEVERAGE > 0 and target_notional > 0:
        eff_lev = target_notional / trader_equity
        if eff_lev > MAX_TARGET_LEVERAGE:
            scale *= MAX_TARGET_LEVERAGE / eff_lev
            logger.warning(
                f"目標有效槓桿 {eff_lev:.1f}x 超過上限 {MAX_TARGET_LEVERAGE:.0f}x，"
                f"倉位縮至 {MAX_TARGET_LEVERAGE/eff_lev:.0%}"
            )
    return scale


def sync_positions(
    api_url: str,
    trader: Trader,
    target_state: dict,
    my_state: dict,
    my_address: str = "",
    protected: Optional[set] = None,
) -> dict:
    """
    同步我的倉位與目標交易員的倉位。

    偵測三種情況：
      - 新倉位：目標有、我沒有 → 開倉
      - 調整大小：目標與我的 size 差距 >2% → 加/減倉
      - 平倉：目標已平、我還有 → 平倉

    protected：抗單保護標的集合；對這些標的只允許減倉/平倉，不新開、不加倉。
    回傳 {"scale": float, "actions": [...]} 。
    """
    protected = protected or set()
    failed_dexs = target_state.get("failed_dexs", set())
    trader_account_value = target_state["account_value"]
    target_positions = target_state["positions"]
    my_positions = my_state["positions"]

    my_equity = my_state.get("account_value", 0.0)
    target_notional = sum(p["notional"] for p in target_positions.values())
    scale = compute_scale_factor(trader_account_value, my_equity, target_notional)
    logger.info(
        f"交易員淨值 ${trader_account_value:,.0f} | "
        f"跟單本金 ${resolve_capital(my_equity):,.0f} | "
        f"比例 {scale:.4f}"
    )

    actions = []

    # ── 1. 目標有倉位的標的 ─────────────────────────────────
    for coin, tgt_pos in target_positions.items():
        target_size = tgt_pos["size"] * scale
        target_side = tgt_pos["side"]
        # 名目槓桿用標的最大值；cross 最省保證金，xyz/onlyIsolated 自動改 isolated
        leverage = trader.entry_leverage(coin)
        is_cross = trader.entry_is_cross(coin)
        mid_px = get_mid_price(api_url, coin) or tgt_pos["entry_px"]
        notional = target_size * mid_px

        if notional < MIN_ORDER_NOTIONAL:
            logger.debug(f"[SKIP] {coin} 名目值 ${notional:.2f} 低於最小值 ${MIN_ORDER_NOTIONAL}")
            continue

        if coin not in my_positions:
            # ── 新開倉 ──（抗單保護：不新建抗單標的部位）
            if coin in protected:
                logger.warning(f"[抗單保護] {coin} 持倉時間異常，跳過新開倉")
                continue
            logger.info(f"[ACTION] 新開倉 {coin} {target_side} size={target_size:.4f} lev={leverage}x")
            is_buy = target_side == "long"
            result = trader.open_position(
                coin, is_buy, target_size, leverage, is_cross,
                entry_px=mid_px, scale=scale, trader_account=trader_account_value,
                my_address=my_address, api_url=api_url,
            )
            actions.append({
                "action": "open", "coin": coin, "side": target_side,
                "size": target_size, "entry_px": mid_px, "result": result,
            })

        else:
            my_pos = my_positions[coin]
            my_size = my_pos["size"]
            my_side = my_pos["side"]
            size_diff_pct = abs(target_size - my_size) / max(my_size, 1e-8)

            # 抗單保護：該標的只允許同向減倉，不加倉、不反向
            if coin in protected and target_side == my_side and target_size > my_size:
                logger.warning(f"[抗單保護] {coin} 持倉時間異常，跳過加倉（{my_size:.4f}→{target_size:.4f}）")
                continue

            if my_side != target_side or size_diff_pct > SIZE_TOLERANCE:
                # ── 調整倉位（方向改變 or 差距 > 容忍度）──
                logger.info(
                    f"[ACTION] 調整 {coin}: "
                    f"我的={my_side} {my_size:.4f} → 目標={target_side} {target_size:.4f} "
                    f"（diff={size_diff_pct:.1%}）"
                )
                trader.adjust_position(
                    coin, my_size, target_size, my_side, target_side, leverage, is_cross,
                    entry_px=mid_px, scale=scale, trader_account=trader_account_value,
                    unrealized_pnl=my_pos.get("unrealized_pnl", 0),
                    my_address=my_address, api_url=api_url,
                )
                actions.append({
                    "action": "adjust", "coin": coin,
                    "from_size": my_size, "to_size": target_size,
                    "from_side": my_side, "to_side": target_side,
                })
            else:
                logger.debug(f"[OK] {coin} 倉位差距 {size_diff_pct:.1%}，無需調整")

    # ── 2. 我有但目標已平的標的 → 跟著平 ─────────────────────
    for coin, my_pos in list(my_positions.items()):
        if coin not in target_positions:
            if _coin_dex(coin) in failed_dexs:
                logger.warning(f"[資料保護] {coin} 所屬 DEX 查詢失敗，本輪跳過平倉")
                continue
            logger.info(f"[ACTION] 目標已平 {coin}，跟著平倉 size={my_pos['size']:.4f}")
            is_buy_close = my_pos["side"] == "long"
            result = trader.close_position(
                coin, is_buy_close, my_pos["size"],
                unrealized_pnl=my_pos.get("unrealized_pnl", 0),
                my_address=my_address,
                api_url=api_url,
            )
            actions.append({"action": "close", "coin": coin, "result": result})

    if not actions:
        logger.info("倉位已同步，無需操作")

    return {"scale": scale, "actions": actions}
