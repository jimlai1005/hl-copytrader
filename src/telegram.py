"""
Telegram 通知模組
所有傳送失敗只 log，不拋出例外，避免因通知失敗影響交易主流程。
"""
import logging
import os
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_API = "https://api.telegram.org/bot{token}/sendMessage"


def _send(text: str) -> None:
    if not _BOT_TOKEN or not _CHAT_ID:
        logger.debug("Telegram 未設定，跳過通知")
        return
    try:
        url = _API.format(token=_BOT_TOKEN)
        resp = requests.post(
            url,
            json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=8,
        )
        if not resp.ok:
            logger.warning(f"Telegram 傳送失敗: {resp.status_code} {resp.text[:200]}")
    except Exception as e:
        logger.warning(f"Telegram 例外: {e}")


def _now() -> str:
    return datetime.now().strftime("%m/%d %H:%M")


def _c(coin: str) -> str:
    """幣名安全顯示：包在 <code> tag，避免 @ / : 在 Telegram 產生誤解析。"""
    return f"<code>{coin}</code>"


# ── 開倉通知 ──────────────────────────────────────────────
def notify_open(coin: str, side: str, size: float, entry_px: float,
                leverage: int, lev_type: str, notional: float,
                scale: float, trader_account: float) -> None:
    side_emoji = "🟢" if side == "long" else "🔴"
    side_zh = "多單 Long" if side == "long" else "空單 Short"
    _send(
        f"【通知】{_c(coin)} 跟單開倉 {side_emoji}\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>方向：</b>{side_zh}\n"
        f"<b>數量：</b>{size:.4f} {_c(coin)}\n"
        f"<b>槓桿：</b>{leverage}x {lev_type.capitalize()}\n"
        f"<b>進場價：</b>${entry_px:,.4f}\n"
        f"<b>名目值：</b>${notional:,.2f}\n"
        f"━━━━━━━━━━\n"
        f"<b>跟單比例：</b>{scale:.2%}\n"
        f"<b>交易員帳戶：</b>${trader_account:,.0f}"
    )


# ── 平倉通知（含實際 P&L）────────────────────────────────
def notify_close(coin: str, side: str, size: float, pnl: float,
                 reason: str = "跟單平倉") -> None:
    pnl_emoji = "💰" if pnl >= 0 else "🔻"
    pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
    pnl_label = "已實現盈虧（含手續費）" if pnl != 0 else "已實現盈虧"
    _send(
        f"【通知】{_c(coin)} {reason} {pnl_emoji}\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>方向：</b>{'多單' if side == 'long' else '空單'}\n"
        f"<b>數量：</b>{size:.4f} {_c(coin)}\n"
        f"<b>{pnl_label}：</b><b>{pnl_str}</b>"
    )


# ── 費率套利通知 ──────────────────────────────────────────
def notify_funding_arb(coin: str, long_side: str, short_side: str,
                       size: float, funding_rate: float,
                       est_daily_usd: float, notional: float) -> None:
    _send(
        f"【通知】{_c(coin)} 套利組合已開倉 📈\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>多方：</b>{long_side}　<b>空方：</b>{short_side}\n"
        f"<b>數量：</b>{size:.4f} {_c(coin)}\n"
        f"<b>名目值：</b>${notional:,.2f}\n"
        f"━━━━━━━━━━\n"
        f"<b>預期費率：</b>{funding_rate:+.4%} / 每8h\n"
        f"<b>預期日收益：</b>${est_daily_usd:,.2f}"
    )


# ── 每日結算摘要 ──────────────────────────────────────────
def notify_daily_summary(account_value: float, daily_pnl: float,
                         positions: dict, funding_earned: float = 0.0) -> None:
    pos_lines = ""
    for coin, pos in positions.items():
        pnl = pos.get("unrealized_pnl", 0)
        sign = "+" if pnl >= 0 else ""
        pos_lines += f"  {_c(coin)} {pos['side']} → {sign}${pnl:,.2f}\n"

    daily_emoji = "📈" if daily_pnl >= 0 else "📉"
    _send(
        f"【每日摘要】跟單帳戶 {daily_emoji}\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>帳戶總值：</b>${account_value:,.2f}\n"
        f"<b>當日盈虧：</b>{'+'  if daily_pnl >= 0 else ''}${daily_pnl:,.2f}\n"
        f"<b>累計費率收益：</b>${funding_earned:,.2f}\n"
        f"━━━━━━━━━━\n"
        f"<b>持倉：</b>\n{pos_lines or '  無持倉'}"
    )


# ── 錯誤警告 ──────────────────────────────────────────────
def alert_error(error_type: str, detail: str, extra: str = "") -> None:
    _send(
        f"【警告】Hyperliquid 跟單失敗！ 🚨\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>錯誤：</b>{error_type}\n"
        f"<b>詳情：</b>{detail}"
        + (f"\n{extra}" if extra else "")
    )


def alert_insufficient_balance(balance: float, required: float, coin: str) -> None:
    _send(
        f"【警告】帳戶餘額不足！ 🚨\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>標的：</b>{_c(coin)}\n"
        f"<b>可用餘額：</b>${balance:,.2f} USDC\n"
        f"<b>所需保證金：</b>${required:,.2f} USDC\n"
        f"請立即充值或縮減跟單比例"
    )


def alert_api_error(status_code: int, message: str) -> None:
    _send(
        f"【警告】Hyperliquid 跟單失敗！ 🚨\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>錯誤代碼：</b>{status_code}\n"
        f"<b>訊息：</b>{message}\n"
        f"請檢查 API Key 或網路連線"
    )


def alert_drawdown(current_value: float, initial: float, drawdown_pct: float) -> None:
    _send(
        f"【警告】帳戶回撤超過上限，已停止跟單！ 🛑\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>初始資金：</b>${initial:,.2f}\n"
        f"<b>當前帳戶：</b>${current_value:,.2f}\n"
        f"<b>回撤幅度：</b>{drawdown_pct:.1%}\n"
        f"請手動確認後重新啟動"
    )


def alert_bot_started(live: bool, capital: float, target: str) -> None:
    mode = "🔴 真實交易" if live else "🟡 乾跑模式"
    _send(
        f"【系統】跟單機器人已啟動 🤖\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>模式：</b>{mode}\n"
        f"<b>跟單資金：</b>${capital:,.0f} USDC\n"
        f"<b>目標交易員：</b><code>{target[:10]}...</code>"
    )


def alert_bot_stopped(reason: str) -> None:
    _send(
        f"【系統】跟單機器人已停止 ⏹\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>原因：</b>{reason}"
    )
