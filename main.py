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

# 確保 src 在路徑中
sys.path.insert(0, str(Path(__file__).parent))

from src.config import (
    TARGET_TRADER, WALLET_PRIVATE_KEY, WALLET_ADDRESS,
    ALLOCATED_CAPITAL, MAX_DRAWDOWN_PCT, LIVE_TRADING,
    POLL_INTERVAL, HL_API_URL, NETWORK
)
from src.monitor import get_trader_state, get_my_state
from src.sync import sync_positions
from src.trader import Trader


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
    """建立 Exchange 物件，需要私鑰。"""
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

    # 只顯示狀態
    if args.status:
        state = get_trader_state(HL_API_URL, TARGET_TRADER)
        print_status(state, f"目標交易員 {TARGET_TRADER[:10]}...")
        return

    # 初始化交易器
    if not is_dry_run:
        exchange, info = build_exchange()
        trader = Trader(exchange, info, live_trading=True)
    else:
        trader = Trader(None, None, live_trading=False)

    # 計算初始帳戶價值（用來計算回撤）
    initial_capital = ALLOCATED_CAPITAL
    prev_target_positions = None
    # 乾跑模式用來追蹤模擬倉位
    simulated_positions: dict = {}

    logger.info(f"開始監控，每 {POLL_INTERVAL} 秒輪詢一次")

    while True:
        try:
            # 取得目標交易員狀態
            target_state = get_trader_state(HL_API_URL, TARGET_TRADER)
            print_status(target_state, f"目標交易員 {TARGET_TRADER[:10]}...")

            # 取得我的帳戶狀態
            if not is_dry_run and WALLET_ADDRESS:
                my_state = get_my_state(HL_API_URL, WALLET_ADDRESS)
                print_status(my_state, "我的帳戶")

                # 回撤檢查
                my_value = my_state["account_value"]
                drawdown = (initial_capital - my_value) / initial_capital
                if drawdown > MAX_DRAWDOWN_PCT:
                    logger.error(
                        f"帳戶回撤 {drawdown:.1%} 超過上限 {MAX_DRAWDOWN_PCT:.0%}，停止跟單！"
                    )
                    break
            else:
                # 乾跑模式：使用模擬倉位狀態
                my_state = {
                    "account_value": ALLOCATED_CAPITAL,
                    "positions": simulated_positions,
                }

            # 同步倉位
            result = sync_positions(
                api_url=HL_API_URL,
                trader=trader,
                target_state=target_state,
                my_state=my_state,
                prev_target_positions=prev_target_positions,
            )

            # 乾跑模式：根據操作結果更新模擬倉位
            if is_dry_run:
                scale = result.get("scale", 0)
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

        except KeyboardInterrupt:
            logger.info("使用者中斷，停止跟單")
            break
        except Exception as e:
            logger.error(f"輪詢發生錯誤: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
