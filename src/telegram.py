"""
Telegram 通知模組
所有傳送失敗只 log，不拋出例外，避免因通知失敗影響交易主流程。
"""
import html as _html
import logging
import os
import time as _time
import requests
from datetime import datetime

from .config import NOTIFY_ORDERS, NOTIFY_OPENS, NOTIFY_VOLATILITY

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
_API = "https://api.telegram.org/bot{token}/sendMessage"

# 相同警告訊息在此秒數內不重複發送（避免如「市場未開盤」每分鐘洗版）
_DEDUP_TTL = 300
_recent_sent = {}


def _send(text: str, dedup_key: str = None) -> None:
    if not _BOT_TOKEN or not _CHAT_ID:
        logger.debug("Telegram 未設定，跳過通知")
        return
    if dedup_key is not None:
        now = _time.time()
        # 順手清掉過期項，避免無限長
        for k in [k for k, t in _recent_sent.items() if now - t > _DEDUP_TTL]:
            _recent_sent.pop(k, None)
        if now - _recent_sent.get(dedup_key, 0) < _DEDUP_TTL:
            return
        _recent_sent[dedup_key] = now
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
    """幣名安全顯示：HTML escape 後包在 <code> tag。"""
    return f"<code>{_html.escape(coin)}</code>"


def _e(text: str) -> str:
    """對 API 回傳的動態字串做 HTML escape，避免破壞 parse_mode=HTML。"""
    return _html.escape(str(text))


# ── 開倉通知（NOTIFY_OPENS）────────────────────────────────
def notify_open(coin: str, side: str, size: float, entry_px: float,
                leverage: int, lev_type: str, notional: float,
                scale: float, trader_account: float) -> None:
    if not NOTIFY_OPENS:
        return
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


# ── 掛單/改單通知（NOTIFY_ORDERS）─────────────────────────
def notify_order_placed(coin: str, is_buy: bool, size: float, px: float,
                        order_type_name: str, reduce_only: bool,
                        notional: float) -> None:
    if not NOTIFY_ORDERS:
        return
    side_emoji = "🟢" if is_buy else "🔴"
    side_zh = "買進 Buy" if is_buy else "賣出 Sell"
    ro = "（減倉 reduce-only）" if reduce_only else ""
    _send(
        f"【掛單】{_c(coin)} 跟單掛單 {side_emoji}\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>方向：</b>{side_zh} {ro}\n"
        f"<b>類型：</b>{_e(order_type_name)}\n"
        f"<b>數量：</b>{size:.4f} {_c(coin)}\n"
        f"<b>掛單價：</b>${px:,.4f}\n"
        f"<b>名目值：</b>${notional:,.2f}"
    )


def notify_order_modified(coin: str, is_buy: bool, size: float, px: float,
                          order_type_name: str, reduce_only: bool) -> None:
    if not NOTIFY_ORDERS:
        return
    side_emoji = "🟢" if is_buy else "🔴"
    side_zh = "買進 Buy" if is_buy else "賣出 Sell"
    ro = "（減倉 reduce-only）" if reduce_only else ""
    _send(
        f"【改單】{_c(coin)} 修改掛單 ✏️\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>方向：</b>{side_zh} {side_emoji} {ro}\n"
        f"<b>類型：</b>{_e(order_type_name)}\n"
        f"<b>新數量：</b>{size:.4f} {_c(coin)}\n"
        f"<b>新掛單價：</b>${px:,.4f}"
    )


# ── 我的帳戶波動權重（NOTIFY_VOLATILITY，每小時最多一則）──────
_vol_last_sent = {"ts": 0.0}


def notify_account_volatility(stats: dict) -> None:
    if not NOTIFY_VOLATILITY or not stats:
        return
    now = _time.time()
    if now - _vol_last_sent["ts"] < 3600:   # 每小時最多發一則
        return
    _vol_last_sent["ts"] = now
    z = stats["z"]
    妖 = "🟢 正常" if z <= 1.5 else ("🟠 偏高" if z <= 2.0 else "🔴 妖")
    _send(
        f"【我的帳戶波動】{妖}\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>今日 |PnL|：</b>${stats['today']:,.0f}\n"
        f"<b>14日均值 μ：</b>${stats['mu']:,.0f}\n"
        f"<b>標準差 σ：</b>${stats['sigma']:,.0f}\n"
        f"<b>Z-Score：</b>{z:.2f}\n"
        f"<b>對應權重：</b>{stats['weight']:.2f}"
    )


def notify_orders_synced(scale: float, eff_lev: float, matched: int,
                         placed: int, cancelled: int, modified: int,
                         pos_actions: int, trader_equity: float,
                         sync_failed: bool = False) -> None:
    head = "【同步】掛單已更新 🔄" if not sync_failed else "【同步】掛單更新（含異常）⚠️"
    _send(
        f"{head}\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>掛單：</b>保留 {matched}、改單 {modified}、新增 {placed}、取消 {cancelled} 筆\n"
        f"<b>部位安全網調整：</b>{pos_actions} 筆\n"
        f"━━━━━━━━━━\n"
        f"<b>有效槓桿：</b>{eff_lev:.2f}x\n"
        f"<b>跟單比例：</b>{scale:.2%}\n"
        f"<b>交易員淨值：</b>${trader_equity:,.0f}"
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


# ── 錯誤警告 ──────────────────────────────────────────────
def alert_error(error_type: str, detail: str, extra: str = "") -> None:
    _send(
        f"【警告】Hyperliquid 跟單失敗！ 🚨\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>錯誤：</b>{_e(error_type)}\n"
        f"<b>詳情：</b>{_e(detail)}"
        + (f"\n{_e(extra)}" if extra else ""),
        dedup_key=f"error:{error_type}:{detail}",
    )


def alert_insufficient_balance(balance: float, required: float, coin: str) -> None:
    _send(
        f"【警告】帳戶餘額不足！ 🚨\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>標的：</b>{_c(coin)}\n"
        f"<b>可用餘額：</b>${balance:,.2f} USDC\n"
        f"<b>所需保證金：</b>${required:,.2f} USDC\n"
        f"請立即充值或縮減跟單比例",
        dedup_key=f"insufficient:{coin}",
    )


def alert_api_error(status_code: int, message: str) -> None:
    _send(
        f"【警告】Hyperliquid 跟單失敗！ 🚨\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>錯誤代碼：</b>{status_code}\n"
        f"<b>訊息：</b>{_e(message)}\n"
        f"請檢查 API Key 或網路連線",
        dedup_key=f"apierr:{message}",
    )


def alert_drawdown(current_value: float, peak: float, drawdown_pct: float) -> None:
    _send(
        f"【警告】帳戶回撤超過上限，已停止跟單！ 🛑\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>近期高點：</b>${peak:,.2f}\n"
        f"<b>當前帳戶：</b>${current_value:,.2f}\n"
        f"<b>回撤幅度：</b>{drawdown_pct:.1%}\n"
        f"請手動確認後重新啟動"
    )


def alert_order_sync_failed(missing: list, extra: list) -> None:
    """掛單同步重試後仍與目標不一致，需人為介入。"""
    lines = ""
    for d in missing:
        px = d.get("limit_px") or d.get("trigger_px") or 0
        side = "買" if d.get("is_buy") else "賣"
        lines += f"  ❌ 缺少：{_c(d['coin'])} {side} {d.get('size', 0):.4f} @ ${px:,.4f}\n"
    for m in extra:
        px = m.get("limit_px") or m.get("trigger_px") or 0
        side = "買" if m.get("is_buy") else "賣"
        lines += f"  ⚠️ 多餘：{_c(m['coin'])} {side} {m.get('size', 0):.4f} @ ${px:,.4f}\n"
    _send(
        f"【警告】掛單同步異常，需人為介入 🛠\n"
        f"<b>時間：</b>{_now()}\n"
        f"重試後仍與目標不一致：\n{lines}"
        f"━━━━━━━━━━\n"
        f"<b>請至 Hyperliquid 手動處理：</b>\n"
        f"• 「缺少」的單 → 手動補掛\n"
        f"• 「多餘」的單 → 手動取消\n"
        f"或重啟服務重試：<code>systemctl restart hl-copytrader</code>"
    )


def notify_holding_protection(coin: str, z: float) -> None:
    """抗單保護觸發：目標持倉時間異常，拒絕複製其補倉。"""
    _send(
        f"【抗單保護】拒絕補倉 🛡\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>標的：</b>{_c(coin)}\n"
        f"<b>持倉時間 Z-Score：</b>{z:.1f}（異常偏高，疑似抗單）\n"
        f"已拒絕複製此標的的新補倉單；其減倉/止盈止損仍會跟。"
    )


def alert_position_too_small(coin: str, notional: float, min_notional: float) -> None:
    """換算後部位低於最小門檻而被跳過。"""
    _send(
        f"【提醒】部位過小已跳過 ℹ️\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>標的：</b>{_c(coin)}\n"
        f"<b>換算名目值：</b>${notional:,.2f}\n"
        f"<b>最小門檻：</b>${min_notional:,.2f}\n"
        f"<b>建議：</b>跟單資金分散到過多標的所致。可考慮提高跟單資金，"
        f"或此標的本就太小、可忽略。"
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
        f"<b>原因：</b>{_e(reason)}"
    )
