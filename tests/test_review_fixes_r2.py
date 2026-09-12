"""Reviewer round-2 修正的回歸測試。"""
import src.telegram as telegram
from src import liquidation as liq, sync, orders, instrument
from src import trader as trader_mod
from src.trader import Trader

REAL_SEND = telegram._send
ME = "0x1A1d5eF3256e1A7de2db2082D7A1eEb976c90111"
NOW = 1_800_000_000.0


class FakeInfo:
    def meta(self, dex=""):
        if dex == "xyz":
            return {"universe": [{"name": "xyz:CL", "szDecimals": 3, "maxLeverage": 20}]}
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


class LevExchange:
    def __init__(self, lev_result):
        self.lev_result = lev_result; self.orders = 0
    def update_leverage(self, leverage, coin, is_cross): return self.lev_result
    def order(self, *a, **k):
        self.orders += 1
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}


def _spec(coin, reduce_only=False):
    return {"coin": coin, "is_buy": False, "size": 1.0, "limit_px": 100.0, "trigger_px": 0.0,
            "reduce_only": reduce_only, "is_trigger": False, "tpsl": None, "is_market": False,
            "tif": "Gtc", "order_type_name": "Limit", "target_leverage": 0, "held_leverage": 0}


# ── C-1：保護中目標翻向 → 平掉我方部位，但不開反向 ─────────────────
def test_protected_reversal_closes_but_does_not_reopen(monkeypatch, dry_trader):
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    closed, opened, adjusted = [], [], []
    monkeypatch.setattr(dry_trader, "close_position", lambda *a, **k: closed.append(a) or {"status": "dry_run"})
    monkeypatch.setattr(dry_trader, "open_position", lambda *a, **k: opened.append(a))
    monkeypatch.setattr(dry_trader, "adjust_position", lambda *a, **k: adjusted.append(a))
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "long", "size": 1000.0, "entry_px": 100.0,
           "leverage": 4, "leverage_type": "isolated", "notional": 100000.0, "unrealized_pnl": 0.0}
    mine = {"xyz:CL": {"side": "short", "size": 1.0, "leverage": 4, "leverage_type": "isolated", "unrealized_pnl": -2.0}}
    res = sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                              {"account_value": 1000.0, "positions": mine}, protected={"xyz:CL"}, scale=0.002)
    assert len(closed) == 1 and closed[0][0] == "xyz:CL" and closed[0][2] == 1.0
    assert opened == [] and adjusted == []
    assert [a["action"] for a in res["actions"]] == ["close"]


# ── C-2：掛單路徑 isolated 槓桿設定失敗 → 不掛 ───────────────────
def test_set_entry_leverage_returns_false_on_isolated_failure(monkeypatch):
    ex = LevExchange({"status": "err", "response": "boom"})
    t = Trader(ex, FakeInfo(), live_trading=True)
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    assert orders._set_entry_leverage(t, _spec("xyz:CL")) is False
    assert orders._set_entry_leverage(t, _spec("BTC")) is True          # cross 失敗仍放行
    assert orders._set_entry_leverage(t, _spec("xyz:CL", reduce_only=True)) is True


def test_reconcile_skips_isolated_order_when_leverage_fails(monkeypatch):
    ex = LevExchange({"status": "err", "response": "boom"})
    t = Trader(ex, FakeInfo(), live_trading=True)
    t._sz_dec = {"xyz:CL": 3, "BTC": 5}
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "notify_order_placed", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "alert_order_sync_failed", lambda *a, **k: None)
    # 驗證重抓：交易所上只有 BTC 那張（CL 被閘門擋掉），CL 會被判「缺少」→ 補缺再被擋 → sync_failed
    mine_btc = {"coin": "BTC", "is_buy": False, "reduce_only": False, "is_trigger": False,
                "limit_px": 100.0, "trigger_px": 0.0, "size": 1.0, "oid": 1, "tpsl": None, "is_market": False}
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [mine_btc])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    monkeypatch.setattr(trader_mod, "_order_rests", lambda *a, **k: True)
    res = orders._reconcile_orders(t, "api", ME, [_spec("xyz:CL"), _spec("BTC")], [])
    assert ex.orders == 1            # 只有 BTC 掛出去；CL 在首掛與補缺兩處都被擋
    assert res["placed"] == 1
    assert res["sync_failed"] is True   # 持續被擋會每輪報同步失敗，屬「大聲失敗」的預期行為


# ── W-3：_send 失敗不佔用 dedup，下一輪可立即補送 ───────────────────
class _Resp:
    def __init__(self, ok): self.ok = ok; self.status_code = 500; self.text = "x"


def test_send_failure_does_not_consume_dedup(monkeypatch):
    monkeypatch.setattr(telegram, "_BOT_TOKEN", "t"); monkeypatch.setattr(telegram, "_CHAT_ID", "c")
    monkeypatch.setattr(telegram, "_recent_sent", {}); monkeypatch.setattr(telegram._time, "sleep", lambda s: None)
    results = iter([_Resp(False), _Resp(True), _Resp(True)])
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: next(results))
    assert REAL_SEND("x", dedup_key="k") is False
    assert REAL_SEND("x", dedup_key="k") is True       # 失敗沒佔用 → 立即補送成功
    assert REAL_SEND("x", dedup_key="k") is False      # 成功後才進入 5 分鐘去重


# ── W-4：held 只在部位本身是 isolated 時才採用 ───────────────────────
def test_held_isolated_leverage_helper():
    assert instrument.held_isolated_leverage({"leverage": 3, "leverage_type": "isolated"}) == 3
    assert instrument.held_isolated_leverage({"leverage": 40, "leverage_type": "cross"}) == 0
    assert instrument.held_isolated_leverage({}) == 0
    assert instrument.held_isolated_leverage(None) == 0


def test_build_desired_ignores_cross_held_leverage(dry_trader):
    dry_trader._sz_dec["xyz:CL"] = 3
    desired, *_ = orders._build_desired(dry_trader, [_spec("xyz:CL")], scale=1.0, protected=set(),
                                        my_positions={"xyz:CL": {"side": "short", "size": 1.0, "leverage": 40, "leverage_type": "cross"}},
                                        target_positions={})
    assert desired[0]["held_leverage"] == 0


def test_safety_net_ignores_cross_held_leverage(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    seen = {}
    monkeypatch.setattr(dry_trader, "adjust_position",
                        lambda coin, cur, tgt, cs, ts, leverage, is_cross, **kw: seen.update(leverage=leverage))
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "short", "size": 1000.0, "entry_px": 100.0,
           "leverage": 10, "leverage_type": "isolated", "notional": 100000.0, "unrealized_pnl": 0.0}
    mine = {"xyz:CL": {"side": "short", "size": 1.0, "leverage": 40, "leverage_type": "cross", "unrealized_pnl": 0.0}}
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                        {"account_value": 1000.0, "positions": mine}, scale=0.002)
    assert seen["leverage"] == 10                       # 不採用 cross 的 40


# ── W-2：熱快取連續失敗要告警 ────────────────────────────────────────
def test_hot_cache_repeated_failures_alert(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}, "failures": 0})
    monkeypatch.setattr(liq, "_alerted", set())
    monkeypatch.setattr(liq._time, "sleep", lambda s: None)
    monkeypatch.setattr(telegram, "alert_liquidated", lambda *a: True)
    fills = [{"coin": "xyz:CL", "time": int((NOW - 100) * 1000), "closedPnl": "-1",
              "liquidation": {"liquidatedUser": ME.lower()}}]
    monkeypatch.setattr(liq, "_post", lambda a, p: fills)
    liq.get_liquidation_cooldowns("api", ME, now=NOW)                 # 熱快取建立
    alerts = []
    monkeypatch.setattr(telegram, "alert_error", lambda t, d, extra="": alerts.append(t))
    def boom(a, p): raise RuntimeError("429")
    monkeypatch.setattr(liq, "_post", boom)
    for i in range(1, 4):
        liq._cache["ts"] = 0.0
        liq.get_liquidation_cooldowns("api", ME, now=NOW + 60 * i)
    assert alerts == ["清算冷卻表連續抓取失敗"]                      # 第 3 次才叫，之後靠 dedup
    liq._cache["ts"] = 0.0
    monkeypatch.setattr(liq, "_post", lambda a, p: fills)
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 300)
    assert liq._cache["failures"] == 0                                 # 成功歸零
