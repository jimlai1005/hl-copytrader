"""
Hyperliquid Copy Trader — 主程式入口

用法:
  python main.py            # 正常跟單模式 (依 .env 設定)
  python main.py --dry-run  # 乾跑模式，只監控不下單
  python main.py --status   # 只顯示目標交易員當前倉位
"""
import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.config import (
    TARGET_TRADER, WALLET_PRIVATE_KEY, WALLET_ADDRESS,
    ALLOCATED_CAPITAL, MAX_DRAWDOWN_PCT, LIVE_TRADING,
    POLL_INTERVAL, HL_API_URL, NETWORK
)
from src.monitor import get_trader_state, get_my_state
from src.sync import sync_positions
from src.trader import Trader
from src import telegram as tg


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
                f"    {coin:8s} {pos['side']:5s}  "
                f"size={pos['size']:.4f}  "
                f"entry=${pos['entry_px']:.4f}  "
                f"lev={pos['leverage']}x{pos['leverage_type'][0].upper()}  "
                f"PnL={pnl_str}"
            )
    print('='*60)


def build_exchange():
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info
    from eth_account import Account

    if not WALLET_PRIVATE_KEY:
        raise ValueError("WALLET_PRIVATE_KEY 未設定，請在 .env 中填入")

    account = Account.from_key(WALLET_PRIVATE_KEY)
    info = Info(HL_API_URL, skip_ws=True)
    exchange = Exchange(account, HL_API_URL, account_address=WALLET_ADDRESS or account.address)
    return exchange, info


def main():
    setup_logging()
    logger = logging.getLogger("main")

    parser = argparse.ArgumentParser(description="Hyperliquid Copy Trader")
    parser.add_argument("--dry-run", action="store_true", help="乾跑模式，不實際下單")
    parser.add_argument("--status", action="store_true", help="顯示狀態後退出")
    args = parser.parse_args()

    is_dry_run = args.dry_run or not LIVE_TRADING

    print("\n" + "="*60)
    print("  Hyperliquid Copy Trader")
    print(f"  目標交易員: {TARGET_TRADER}")
    print(f"  跟單資金:   ${ALLOCATED_CAPITAL:,.0f} USDC")
    print(f"  網路:       {NETWORK.upper()}")
    print(f"  模式:       {'🔴 真實交易' if not is_dry_run else '🟡 乾跑 (不下單)'}")
    print(f"  最大回撤:   {MAX_DRAWDOWN_PCT:.0%}")
    print("="*60 + "\n")

    if args.status:
        state = get_trader_state(HL_API_URL, TARGET_TRADER)
        print_status(state, f"目標交易員 {TARGET_TRADER[:10]}...")
        return

    # 初始化
    if not is_dry_run:
        try:
            exchange, info = build_exchange()
            trader = Trader(exchange, info, live_trading=True)
        except Exception as e:
            tg.alert_error("初始化失敗", str(e), "請確認私鑰與地址設定正確")
            logger.critical(f"初始化失敗: {e}")
            sys.exit(1)
    else:
        trader = Trader(None, None, live_trading=False)

    tg.alert_bot_started(not is_dry_run, ALLOCATED_CAPITAL, TARGET_TRADER)

    initial_capital = ALLOCATED_CAPITAL
    prev_target_positions = None
    simulated_positions: dict = {}
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 5

    logger.info(f"開始監控，每 {POLL_INTERVAL} 秒輪詢一次")

    try:
        while True:
            try:
                target_state = get_trader_state(HL_API_URL, TARGET_TRADER)
                print_status(target_state, f"目標交易員 {TARGET_TRADER[:10]}...")

                if not is_dry_run and WALLET_ADDRESS:
                    my_state = get_my_state(HL_API_URL, WALLET_ADDRESS)
                    print_status(my_state, "我的帳戶")

                    my_value = my_state["account_value"]
                    drawdown = (initial_capital - my_value) / initial_capital
                    if drawdown > MAX_DRAWDOWN_PCT:
                        logger.error(f"帳戶回撤 {drawdown:.1%} 超過上限，停止跟單！")
                        tg.alert_drawdown(my_value, initial_capital, drawdown)
                        break
                else:
                    my_state = {
                        "account_value": ALLOCATED_CAPITAL,
                        "positions": simulated_positions,
                    }

                result = sync_positions(
                    api_url=HL_API_URL,
                    trader=trader,
                    target_state=target_state,
                    my_state=my_state,
                    my_address=WALLET_ADDRESS if not is_dry_run else "",
                    prev_target_positions=prev_target_positions,
                )

                if is_dry_run:
                    for action in result.get("actions", []):
                        coin = action["coin"]
                        if action["action"] == "open":
                            tgt = target_state["positions"].get(coin, {})
                            simulated_positions[coin] = {
                                "coin": coin,
                                "side": tgt.get("side", "long"),
                                "size": action["size"],
                                "entry_px": tgt.get("entry_px", 0),
                                "leverage": tgt.get("leverage", 1),
                                "leverage_type": tgt.get("leverage_type", "cross"),
                                "notional": action["size"] * tgt.get("entry_px", 0),
                                "unrealized_pnl": 0,
                            }
                        elif action["action"] == "close":
                            simulated_positions.pop(coin, None)
                        elif action["action"] == "adjust":
                            if coin in simulated_positions:
                                simulated_positions[coin]["size"] = action["to_size"]

                prev_target_positions = target_state["positions"]
                consecutive_errors = 0

            except KeyboardInterrupt:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"輪詢錯誤 ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}",
                             exc_info=True)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    tg.alert_error(
                        "連續錯誤，已暫停",
                        str(e),
                        f"連續失敗 {MAX_CONSECUTIVE_ERRORS} 次，請檢查網路或 API 狀態"
                    )
                    # systemd RestartSec 會在重啟前等待，此處直接退出讓 systemd 重啟
                    sys.exit(1)

            time.sleep(POLL_INTERVAL)

    except KeyboardInterrupt:
        logger.info("使用者中斷")
        tg.alert_bot_stopped("使用者手動停止")


if __name__ == "__main__":
    main()
