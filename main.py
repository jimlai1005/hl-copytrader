"""
Hyperliquid Copy Trader — 主程式入口

策略：每小時在 CHECK_MINUTE（預設 :55）鏡像目標交易員的「掛單」，
      提早 5 分鐘掛上對應限價單，並以部位安全網跟平。

用法:
  python main.py            # 正常跟單模式 (依 .env 設定)
  python main.py --dry-run  # 乾跑模式，只監控不下單
  python main.py --status   # 只顯示目標交易員當前倉位與掛單後退出
  python main.py --once     # 立即執行一次同步後退出（測試用）
"""
import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import (
    TARGET_TRADER, WALLET_PRIVATE_KEY, WALLET_ADDRESS,
    ALLOCATED_CAPITAL, MAX_DRAWDOWN_PCT, LIVE_TRADING,
    CHECK_MINUTE, HL_API_URL, NETWORK, TARGET_EQUITY_INCLUDE_SPOT,
    OFFHOURS_SYNC_MODE,
)
from src.monitor import (
    get_trader_state, get_my_state, get_account_equity,
    get_trader_open_orders, get_my_open_orders,
)
from src.orders import sync_open_orders
from src.trader import Trader
from src.market import is_us_active_hours
from src import telegram as tg
from src import weight


def setup_logging():
    log_dir = Path(__file__).parent / "logs"
    log_dir.mkdir(exist_ok=True)

    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_dir / "copytrader.log", encoding="utf-8"),
        ],
    )


def print_status(state: dict, label: str = ""):
    print(f"\n{'='*60}")
    if label:
        print(f"  {label}")
    print(f"  帳戶價值: ${state['account_value']:,.2f} USDC")
    positions = state["positions"]
    if not positions:
        print("  目前無持倉")
    else:
        print(f"  持倉 ({len(positions)} 個):")
        for coin, pos in positions.items():
            pnl = pos["unrealized_pnl"]
            pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
            print(
                f"    {coin:10s} {pos['side']:5s}  "
                f"size={pos['size']:.4f}  "
                f"entry=${pos['entry_px']:.4f}  "
                f"lev={pos['leverage']}x{pos['leverage_type'][0].upper()}  "
                f"PnL={pnl_str}"
            )
    print('='*60)


def print_orders(orders: list, label: str = ""):
    print(f"\n{'-'*60}")
    if not orders:
        print(f"  {label} 無掛單")
    else:
        print(f"  {label} 掛單 ({len(orders)} 筆):")
        for o in orders:
            side = "買" if o["is_buy"] else "賣"
            px = o["limit_px"] or o["trigger_px"]
            ro = " [reduceOnly]" if o["reduce_only"] else ""
            print(
                f"    {o['coin']:10s} {side} {o['size']:.4f} @ "
                f"${px:,.4f}  {o['order_type_name']}{ro}"
            )
    print('-'*60)


def build_exchange():
    logger = logging.getLogger(__name__)

    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from eth_account import Account

    if not WALLET_PRIVATE_KEY:
        raise ValueError("WALLET_PRIVATE_KEY 未設定，請在 .env 中填入")

    account = Account.from_key(WALLET_PRIVATE_KEY)
    info = Info(HL_API_URL, skip_ws=True)

    # 嘗試同時載入預設 DEX 與 xyz DEX（美股永續）。
    try:
        exchange = Exchange(
            account, HL_API_URL,
            account_address=WALLET_ADDRESS or account.address,
            perp_dexs=["", "xyz"],
        )
        logger.info("Exchange 已載入 xyz DEX 支援")
    except Exception as e:
        logger.warning(f"xyz DEX 初始化失敗，降級為預設 DEX (xyz 股票將無法跟單): {e}")
        exchange = Exchange(account, HL_API_URL, account_address=WALLET_ADDRESS or account.address)

    return exchange, info


def seconds_until_next_minute() -> float:
    """計算距離下一個整分鐘（:00 秒）的秒數，讓同步對齊分鐘邊界。"""
    now = datetime.now()
    nxt = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
    return max(1.0, (nxt - now).total_seconds())


def _minute_key(dt: datetime) -> tuple:
    """以「年月日時分」為鍵，避免同一分鐘內重複執行。"""
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute)


def run_sync(trader, is_dry_run, orders_only=False) -> str:
    """
    執行一次完整同步。
    orders_only=True 時只做掛單鏡像，跳過部位安全網（不以市價接部位）。
    回傳狀態字串："ok" | "drawdown"。
    例外往外拋給呼叫端計算連續錯誤。
    """
    logger = logging.getLogger("sync")

    # 1. 目標交易員的部位與掛單（分母只看目標 perp 槓桿，視 TARGET_EQUITY_INCLUDE_SPOT 決定）
    target_state = get_trader_state(
        HL_API_URL, TARGET_TRADER, include_spot=TARGET_EQUITY_INCLUDE_SPOT
    )
    target_orders, order_failed_dexs = get_trader_open_orders(HL_API_URL, TARGET_TRADER)
    target_state["failed_dexs"] = target_state.get("failed_dexs", set()) | order_failed_dexs
    print_status(target_state, f"目標交易員 {TARGET_TRADER[:10]}...")
    print_orders(target_orders, "目標交易員")

    # 2. 我的帳戶（權益給本金基準與回撤；dry-run 也抓真實權益、但部位用空以預覽全新）
    my_state_real = get_my_state(HL_API_URL, WALLET_ADDRESS) if WALLET_ADDRESS else None
    my_equity = my_state_real["account_value"] if my_state_real else max(ALLOCATED_CAPITAL, 0.0)

    if not is_dry_run and WALLET_ADDRESS:
        my_state = my_state_real
        # 帳戶權益改用 portfolio 的「總帳戶淨值」(unified 帳戶權威值，含 spot 抵押)，
        # 取代 get_my_state 的 perp 子帳 accountValue（會少算 spot → 顯示與本金基準偏低）。
        # 同一個 current_equity 也供下方回撤使用，避免重複呼叫。
        current_equity, peak = get_account_equity(HL_API_URL, WALLET_ADDRESS)
        if current_equity > 0:
            my_state["account_value"] = current_equity
            my_equity = current_equity
        my_orders = get_my_open_orders(HL_API_URL, WALLET_ADDRESS)
        print_status(my_state, "我的帳戶")
        print_orders(my_orders, "我的帳戶")

        # 回撤保護：當前與高點同取自 portfolio 的「總帳戶淨值」。
        peak = max(peak, current_equity)
        if peak > 0:
            drawdown = (peak - current_equity) / peak
            if drawdown > MAX_DRAWDOWN_PCT:
                logger.error(
                    f"帳戶回撤 {drawdown:.1%}（高點 ${peak:,.0f} → 現值 ${current_equity:,.0f}）"
                    f"超過上限，停止跟單！"
                )
                tg.alert_drawdown(current_equity, peak, drawdown)
                return "drawdown"
    else:
        my_state = {"account_value": my_equity, "positions": {}}
        my_orders = []

    # 2b. 我的帳戶波動統計（console 每次印；Telegram 每小時一則）
    if WALLET_ADDRESS:
        mstats = weight.get_vol_stats(WALLET_ADDRESS)
        if mstats:
            print(f"\n  我的帳戶波動：今日|PnL|=${mstats['today']:,.0f} "
                  f"μ=${mstats['mu']:,.0f} σ=${mstats['sigma']:,.0f} "
                  f"Z={mstats['z']:.2f} → 權重 {mstats['weight']:.2f}（基於{mstats['days']}天）")
            tg.notify_account_volatility(mstats)

    # 3. 鏡像掛單（+ 部位安全網，除非 orders_only）
    result = sync_open_orders(
        api_url=HL_API_URL,
        trader=trader,
        target_state=target_state,
        my_state=my_state,
        target_orders=target_orders,
        my_orders=my_orders,
        my_address=WALLET_ADDRESS if not is_dry_run else "",
        skip_safety_net=orders_only,
    )

    # 有任何動作（或同步失敗）才發摘要，避免洗版
    if (result["placed"] or result["cancelled"] or result["modified"]
            or result["pos_actions"] or result["sync_failed"]):
        tg.notify_orders_synced(
            result["scale"], result["eff_lev"], result["matched"],
            result["placed"], result["cancelled"], result["modified"],
            result["pos_actions"], result["trader_equity"], result["sync_failed"],
        )
    return "ok"


def main():
    setup_logging()
    logger = logging.getLogger("main")

    parser = argparse.ArgumentParser(description="Hyperliquid Copy Trader")
    parser.add_argument("--dry-run", action="store_true", help="乾跑模式，不實際下單")
    parser.add_argument("--status", action="store_true", help="顯示狀態與掛單後退出")
    parser.add_argument("--once", action="store_true", help="立即同步一次後退出")
    parser.add_argument("--orders-only", action="store_true",
                        help="只鏡像掛單，跳過部位安全網（不以市價接部位），適合溫和接手")
    args = parser.parse_args()

    is_dry_run = args.dry_run or not LIVE_TRADING
    orders_only = args.orders_only

    cap_desc = f"${ALLOCATED_CAPITAL:,.0f} USDC" if ALLOCATED_CAPITAL > 0 else "自動（帳戶當前權益）"
    offhours_desc = "每 5 分鐘" if OFFHOURS_SYNC_MODE == "5min" else f"每小時 :{CHECK_MINUTE:02d}"
    print("\n" + "="*60)
    print("  Hyperliquid Copy Trader（掛單跟隨模式）")
    print(f"  目標交易員: {TARGET_TRADER}")
    print(f"  跟單資金:   {cap_desc}")
    print(f"  網路:       {NETWORK.upper()}")
    print(f"  模式:       {'🔴 真實交易' if not is_dry_run else '🟡 乾跑 (不下單)'}")
    print(f"  檢查時點:   美股活躍時段(盤前起)每分鐘 / 其餘 {offhours_desc}")
    print(f"  最大回撤:   {MAX_DRAWDOWN_PCT:.0%}")
    if orders_only:
        print(f"  跟單範圍:   ⚠️ 僅掛單鏡像（不跑部位安全網，不市價接部位）")
    print("="*60 + "\n")

    if args.status:
        state = get_trader_state(
            HL_API_URL, TARGET_TRADER, include_spot=TARGET_EQUITY_INCLUDE_SPOT
        )
        orders, _failed = get_trader_open_orders(HL_API_URL, TARGET_TRADER)
        print_status(state, f"目標交易員 {TARGET_TRADER[:10]}...")
        print_orders(orders, "目標交易員")
        return

    # 初始化交易執行器
    if not is_dry_run:
        try:
            exchange, info = build_exchange()
            trader = Trader(exchange, info, live_trading=True)
        except Exception as e:
            tg.alert_error("初始化失敗", str(e), "請確認私鑰與地址設定正確")
            logger.critical(f"初始化失敗: {e}")
            sys.exit(1)
    else:
        # 乾跑也建立唯讀 Info（不下單），讓 size 精度與最大槓桿能正確查詢、log 顯示真實倍率
        info = None
        try:
            from hyperliquid.info import Info
            info = Info(HL_API_URL, skip_ws=True)
        except Exception as e:
            logger.warning(f"乾跑建立 Info 失敗，槓桿/精度將用預設值: {e}")
        trader = Trader(None, info, live_trading=False)

    tg.alert_bot_started(not is_dry_run, ALLOCATED_CAPITAL, TARGET_TRADER)

    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 5

    # --once：立即同步一次後退出
    if args.once:
        try:
            run_sync(trader, is_dry_run, orders_only)
        except Exception as e:
            logger.error(f"同步錯誤: {e}", exc_info=True)
            sys.exit(1)
        return

    offhours_desc = "每 5 分鐘" if OFFHOURS_SYNC_MODE == "5min" else f"每小時 :{CHECK_MINUTE:02d}"
    logger.info(f"啟動完成：美股活躍時段(盤前起)每分鐘同步、其餘 {offhours_desc} 同步")

    try:
        # 啟動時先同步一次，避免等待
        logger.info("啟動初始同步...")
        try:
            status = run_sync(trader, is_dry_run, orders_only)
            if status == "drawdown":
                tg.alert_bot_stopped("回撤超過上限")
                return
        except Exception as e:
            logger.error(f"初始同步失敗（將於下個排程重試）: {e}", exc_info=True)

        # 已在這分鐘跑過初始同步，避免迴圈立刻重跑同一分鐘
        last_run_key = _minute_key(datetime.now())
        prev_active = None

        # 每分鐘醒來判斷：活躍時段(盤前起)→每分鐘；其餘→依 OFFHOURS_SYNC_MODE
        while True:
            now = datetime.now()
            active = is_us_active_hours(now.astimezone())

            if active != prev_active:
                logger.info(
                    "美股活躍時段(盤前起) → 切換為每分鐘同步" if active
                    else f"美股非活躍時段 → 切換為 {offhours_desc} 同步"
                )
                prev_active = active

            if active:
                should_run = True
            elif OFFHOURS_SYNC_MODE == "5min":
                should_run = (now.minute % 5 == 0)
            else:  # hourly
                should_run = (now.minute == CHECK_MINUTE)
            run_key = _minute_key(now)

            if should_run and run_key != last_run_key:
                last_run_key = run_key
                try:
                    status = run_sync(trader, is_dry_run, orders_only)
                    if status == "drawdown":
                        tg.alert_bot_stopped("回撤超過上限")
                        break
                    consecutive_errors = 0
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    consecutive_errors += 1
                    logger.error(
                        f"同步錯誤 ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}",
                        exc_info=True,
                    )
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        tg.alert_error(
                            "連續錯誤，已暫停",
                            str(e),
                            f"連續失敗 {MAX_CONSECUTIVE_ERRORS} 次，請檢查網路或 API 狀態",
                        )
                        sys.exit(1)

            time.sleep(seconds_until_next_minute())

    except KeyboardInterrupt:
        logger.info("使用者中斷")
        tg.alert_bot_stopped("使用者手動停止")


if __name__ == "__main__":
    main()
