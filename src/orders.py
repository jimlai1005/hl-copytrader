"""
掛單跟隨策略（open-orders mirror）+ 部位安全網。

每小時在 CHECK_MINUTE 觸發一次，流程：
  A. 掛單對帳（reconcile）：
     - 以 diff 比對「我的掛單」與「目標掛單（依比例縮放）」。
     - 完全相符的單（含 reduce-only / TP-SL）保留不動，避免撤掉止損造成空窗。
     - 需新增的單：先掛上、確認成功，再取消已不存在的舊單（先掛後取，避免保護空窗）。
     - 同步後重新抓取我的掛單逐張核對；不符則重試一次，仍不符發出人為介入警告。
  B. 部位安全網（position sync）：
     - 比對「我的實際部位」與「目標實際部位（依比例縮放）」。
     - 目標全平→全平；部分平→等比例部分平；部分建倉→跟著建；方向反轉→平掉反向再開。
     - 用於跟上目標以「市價」造成、掛單看不到的部位變動。

比例分母用「本金」(帳戶淨值排除未實現損益)，使下單金額反映真實有效槓桿。
"""
import logging
import time

from .config import (
    MIN_ORDER_NOTIONAL, HOLDING_PROTECTION_ENABLED, TARGET_TRADER, SIZE_TOLERANCE,
)
from .monitor import get_my_open_orders
from .sync import compute_scale_factor, sync_positions
from .trader import Trader, _round_size, _is_spot_coin, _coin_dex
from .protection import get_anti_holding_flags
from . import telegram as tg

logger = logging.getLogger(__name__)

# 結算後等待交易所反映的秒數（驗證前）
SETTLE_SECONDS = 2


def _prices_equal(a: float, b: float, rel: float = 1e-4) -> bool:
    if a == 0 and b == 0:
        return True
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-8)


def _orders_match(desired: dict, mine: dict) -> bool:
    """判斷我的一張掛單是否等同於某個目標縮放後的掛單。"""
    if desired["coin"] != mine["coin"]:
        return False
    if desired["is_buy"] != mine["is_buy"]:
        return False
    if desired["reduce_only"] != mine["reduce_only"]:
        return False
    if desired["is_trigger"] != mine["is_trigger"]:
        return False

    d_px = desired["trigger_px"] if desired["is_trigger"] else desired["limit_px"]
    m_px = mine["trigger_px"] if mine["is_trigger"] else mine["limit_px"]
    if not _prices_equal(d_px, m_px):
        return False

    if desired["is_trigger"]:
        if desired["tpsl"] != mine["tpsl"]:
            return False
        if desired["is_market"] != mine["is_market"]:
            return False
        # 觸發限價單還要比對限價
        if not desired["is_market"] and not _prices_equal(desired["limit_px"], mine["limit_px"]):
            return False

    my_size = mine["size"]
    if my_size <= 0:
        return False
    if abs(desired["size"] - my_size) / max(my_size, 1e-8) > SIZE_TOLERANCE:
        return False
    return True


def _build_desired(trader: Trader, target_orders: list, scale: float,
                   protected: set = None, my_positions: dict = None) -> tuple:
    """
    將目標掛單縮放成「我方期望掛單規格」清單。
    回傳 (desired_specs, skipped_small, skipped_spot, skipped_protected)。
      - skipped_small：名目值過小被跳過的 [(coin, notional)]
      - skipped_spot：現貨標的（不支援跟單）被跳過的 [coin]
      - skipped_protected：抗單保護下拒絕補倉(非 reduce-only)被跳過的 [coin]
    現貨單先排除避免下單錯誤；抗單標的的補倉單(非 reduce-only)排除、但保留其減倉/止盈止損單。
    """
    protected = protected or set()
    my_positions = my_positions or {}
    desired = []
    skipped_small = []
    skipped_spot = []
    skipped_protected = []
    for o in target_orders:
        coin = o["coin"]
        if _is_spot_coin(coin):
            skipped_spot.append(coin)
            continue
        if coin in protected and not o["reduce_only"]:
            skipped_protected.append(coin)   # 抗單保護：不跟補倉，但保留減倉/止盈止損
            continue
        if o["reduce_only"] and coin not in my_positions:
            continue   # G4: 沒有對應部位的 reduce-only 單會被交易所拒，直接跳過
        sz_dec = trader._get_sz_decimals(coin)
        size = _round_size(o["size"] * scale, sz_dec)
        px = o["limit_px"] or o["trigger_px"]
        if size <= 0 or px <= 0:
            continue
        notional = size * px
        if notional < MIN_ORDER_NOTIONAL:
            skipped_small.append((coin, notional))
            continue
        desired.append({
            "coin": coin,
            "is_buy": o["is_buy"],
            "size": size,
            "limit_px": o["limit_px"],
            "trigger_px": o["trigger_px"],
            "reduce_only": o["reduce_only"],
            "is_trigger": o["is_trigger"],
            "tpsl": o["tpsl"],
            "is_market": o["is_market"],
            "tif": o["tif"],
            "order_type_name": o["order_type_name"],
        })
    return desired, skipped_small, skipped_spot, skipped_protected


def _slot_key(o: dict) -> tuple:
    """「同一張概念上的單」的判定鍵：同標的/方向/減倉旗標/觸發類型(+tp/sl)。
    同 slot 的單可用 modify 就地改價/量，不需取消重掛。"""
    return (o["coin"], o["is_buy"], bool(o["reduce_only"]),
            bool(o["is_trigger"]), o["tpsl"] if o["is_trigger"] else None)


def _ref_px(o: dict) -> float:
    return o["trigger_px"] if o["is_trigger"] else o["limit_px"]


def _plan(desired: list, my_orders: list) -> tuple:
    """
    規劃對帳動作，影響由小到大：
      1. 完全相同 → 保留不動（matched）
      2. 同 slot 但價/量不同 → modify 就地改（影響最小）
      3. 目標多出來的 → place 新掛
      4. 我多出來的 → cancel 取消
    回傳 (modifies[(oid, spec)], to_place[spec], to_cancel[order], matched)。
    """
    # 1. 完全相符
    used = set()
    matched = 0
    rem_desired = []
    for d in desired:
        hit = next((m for m in my_orders
                    if m["oid"] not in used and _orders_match(d, m)), None)
        if hit:
            used.add(hit["oid"])
            matched += 1
        else:
            rem_desired.append(d)
    rem_mine = [m for m in my_orders if m["oid"] not in used]

    # 2~4. 依 slot 配對：同 slot 內依參考價排序後逐一對應
    from collections import defaultdict
    mine_by_slot = defaultdict(list)
    for m in rem_mine:
        mine_by_slot[_slot_key(m)].append(m)
    des_by_slot = defaultdict(list)
    for d in rem_desired:
        des_by_slot[_slot_key(d)].append(d)

    modifies, to_place, to_cancel = [], [], []
    for slot in set(mine_by_slot) | set(des_by_slot):
        ms = sorted(mine_by_slot.get(slot, []), key=_ref_px)
        ds = sorted(des_by_slot.get(slot, []), key=_ref_px)
        for i, d in enumerate(ds):
            if i < len(ms):
                modifies.append((ms[i]["oid"], d))   # 配到 → 改單
            else:
                to_place.append(d)                    # 目標較多 → 新掛
        for j in range(len(ds), len(ms)):
            to_cancel.append(ms[j])                   # 我較多 → 取消
    return modifies, to_place, to_cancel, matched


def _set_entry_leverage(trader: Trader, desired: dict) -> None:
    """進場單（非 reduce-only）下單前設定名目槓桿。預設 cross 最省保證金，
    xyz/onlyIsolated 資產自動改 isolated（不支援 cross）。"""
    if desired["reduce_only"]:
        return
    coin = desired["coin"]
    trader.set_leverage(coin, trader.entry_leverage(coin), trader.entry_is_cross(coin))


def _reconcile_orders(trader: Trader, api_url: str, my_address: str,
                      desired: list, my_orders: list) -> dict:
    """
    掛單對帳，影響/風險由小到大：
      1. 相同的單保留不動。
      2. 同 slot 但價量不同 → 先試 modify 就地改（影響最小、保留排隊優先權）。
         modify 失敗（如 Hyperliquid modify 內部先掛新單→保證金不足）則退回老方法：
         取消舊單 → 連同新單一起在步驟 3 重掛（先取消才釋放保證金）。
      3. 取消（改單退回的舊單 + 目標已無的舊單，先釋放保證金）→ 再掛所有新單。
    之後驗證 → 重試一次 → 仍失敗發警告。
    回傳 {"placed","cancelled","modified","matched","sync_failed"}。
    """
    modifies, to_place, to_cancel, matched = _plan(desired, my_orders)

    # ── 1. 就地改單；失敗的退回「取消舊單 + 重掛新單」──────────
    modified = 0
    fallback = []   # modify 失敗 → (舊單 oid, coin, 新單 spec)
    for oid, spec in modifies:
        _set_entry_leverage(trader, spec)   # reduce-only 會自動略過
        if trader.modify_order(oid, spec):
            modified += 1
        else:
            logger.info(f"改單 {spec['coin']} oid={oid} 失敗，退回先取消再重掛")
            fallback.append((oid, spec["coin"], spec))

    # ── 2. 先取消（改單退回的舊單 + 目標已無的舊單）釋放保證金 ──
    cancelled = 0
    for oid, coin, _spec in fallback:
        if trader.cancel_one(coin, oid):
            cancelled += 1
    for m in to_cancel:
        if trader.cancel_one(m["coin"], m["oid"]):
            cancelled += 1

    # ── 3. 後掛新單（目標新增的 + 改單退回的）保證金已釋放 ──────
    placed = 0
    for d in to_place + [spec for _oid, _coin, spec in fallback]:
        _set_entry_leverage(trader, d)
        ok, _ = trader.place_order(d)
        if ok:
            placed += 1

    # ── 4. 驗證（僅 live）→ 不符先撤多再補缺 → 仍不符發警告 ──
    sync_failed = False
    if trader.live_trading and my_address:
        time.sleep(SETTLE_SECONDS)
        after = get_my_open_orders(api_url, my_address)
        missing = [d for d in desired if not any(_orders_match(d, m) for m in after)]
        extra = [m for m in after if not any(_orders_match(d, m) for d in desired)]

        if missing or extra:
            logger.warning(f"掛單驗證不符：缺少 {len(missing)}、多餘 {len(extra)}，重試一次")
            for m in extra:                      # 先撤多（釋放保證金）
                if trader.cancel_one(m["coin"], m["oid"]):
                    cancelled += 1
            for d in missing:                    # 再補缺
                _set_entry_leverage(trader, d)
                ok, _ = trader.place_order(d)
                if ok:
                    placed += 1

            time.sleep(SETTLE_SECONDS)
            after = get_my_open_orders(api_url, my_address)
            missing = [d for d in desired if not any(_orders_match(d, m) for m in after)]
            extra = [m for m in after if not any(_orders_match(d, m) for d in desired)]
            if missing or extra:
                sync_failed = True
                logger.error(f"掛單重試後仍不符：缺少 {len(missing)}、多餘 {len(extra)}，發警告")
                tg.alert_order_sync_failed(missing, extra)

    return {"placed": placed, "cancelled": cancelled, "modified": modified,
            "matched": matched, "sync_failed": sync_failed}


def sync_open_orders(
    api_url: str,
    trader: Trader,
    target_state: dict,
    my_state: dict,
    target_orders: list,
    my_orders: list,
    my_address: str = "",
    skip_safety_net: bool = False,
) -> dict:
    """
    主同步入口：掛單對帳 +（除非 skip_safety_net）部位安全網。
    skip_safety_net=True 時只做掛單鏡像，不以市價接部位（溫和接手用）。
    回傳統計 dict。
    """
    target_positions = target_state["positions"]

    # 分母用「帳戶淨值 equity（含未實現損益）」，使我方保證金使用率自動等於目標，
    # 不會在目標有大浮盈時被過度放大而保證金不足。
    trader_equity = target_state["account_value"]
    total_notional = sum(p["notional"] for p in target_positions.values())
    scale = compute_scale_factor(
        trader_equity, my_state.get("account_value", 0.0), total_notional
    )
    eff_lev = (total_notional / trader_equity) if trader_equity > 0 else 0
    logger.info(
        f"交易員淨值 ${trader_equity:,.0f} | 有效槓桿(對淨值) {eff_lev:.2f}x | "
        f"跟單比例 {scale:.4f} | "
        f"目標掛單 {len(target_orders)} 筆 | 我的掛單 {len(my_orders)} 筆"
    )

    # 抗單保護（預設關閉）：偵測目標持倉時間 Z-Score 異常的標的，拒絕補倉
    protected = {}
    if HOLDING_PROTECTION_ENABLED:
        protected = get_anti_holding_flags(TARGET_TRADER, target_positions)

    # ── A. 掛單對帳 ────────────────────────────────────────
    desired, skipped_small, skipped_spot, skipped_protected = _build_desired(
        trader, target_orders, scale, set(protected), my_state.get("positions", {})
    )
    for coin, notional in skipped_small:
        logger.info(f"[SKIP] {coin} 換算名目值 ${notional:.2f} < ${MIN_ORDER_NOTIONAL}，跳過掛單")
        tg.alert_position_too_small(coin, notional, MIN_ORDER_NOTIONAL)
    if skipped_spot:
        # 現貨不支援跟單，數量差異屬正常，只標記、不發警告
        logger.info(f"[現貨] 略過 {len(skipped_spot)} 筆現貨單(不支援跟單，數量差異屬正常): {skipped_spot}")
    for coin in set(skipped_protected):
        z = protected.get(coin, 0)
        logger.warning(f"[抗單保護] 拒絕複製 {coin} 補倉單 (持倉時間 Z={z:.1f})")
        tg.notify_holding_protection(coin, z)

    failed_dexs = target_state.get("failed_dexs", set())
    my_orders = [m for m in my_orders if _coin_dex(m["coin"]) not in failed_dexs]

    rec = _reconcile_orders(trader, api_url, my_address, desired, my_orders)

    # ── B. 部位安全網（跟上市價造成的部分平/建倉）──────────
    if skip_safety_net:
        logger.info("orders-only 模式：跳過部位安全網（不以市價接部位）")
        pos_actions = []
    else:
        pos_result = sync_positions(
            api_url=api_url,
            trader=trader,
            target_state=target_state,
            my_state=my_state,
            my_address=my_address,
            protected=set(protected),
        )
        pos_actions = pos_result.get("actions", [])

    result = {
        "scale": scale,
        "eff_lev": eff_lev,
        "placed": rec["placed"],
        "cancelled": rec["cancelled"],
        "modified": rec["modified"],
        "matched": rec["matched"],
        "sync_failed": rec["sync_failed"],
        "pos_actions": len(pos_actions),
        "trader_equity": trader_equity,
    }
    logger.info(
        f"同步完成：掛單(保留 {rec['matched']}、改單 {rec['modified']}、"
        f"新增 {rec['placed']}、取消 {rec['cancelled']})"
        f" | 部位調整 {len(pos_actions)} 筆"
        + ("（⚠掛單同步失敗）" if rec["sync_failed"] else "")
    )
    return result
