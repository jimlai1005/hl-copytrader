"""Reviewer round-1 修正的回歸測試。每個測試對應一個 finding。"""
import pytest

from src import liquidation as liq, sync, orders, telegram
from src import trader as trader_mod
from src.trader import Trader

ME = "0x1A1d5eF3256e1A7de2db2082D7A1eEb976c90111"
NOW = 1_800_000_000.0


class FakeInfo:
    def meta(self, dex=""):
        if dex == "xyz":
            return {"universe": [{"name": "xyz:CL", "szDecimals": 3, "maxLeverage": 20}]}
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


class LevExchange:
    def __init__(self, lev_result):
        self.lev_result = lev_result; self.opened = 0
    def update_leverage(self, leverage, coin, is_cross): return self.lev_result
    def market_open(self, *a, **k):
        self.opened += 1
        return {"status": "ok", "response": {"data": {"statuses": [{}]}}}


# ── C2：冷快取 + 抓取失敗 → 重試、仍失敗要大聲叫 ─────────────────
def test_cold_cache_fetch_failure_retries_then_alerts(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}})
    monkeypatch.setattr(liq, "_alerted", set())
    monkeypatch.setattr(liq._time, "sleep", lambda s: None)
    calls = {"n": 0}
    def boom(api_url, payload):
        calls["n"] += 1
        raise RuntimeError("reset")
    monkeypatch.setattr(liq, "_post", boom)
    alerts = []
    monkeypatch.setattr(telegram, "alert_error", lambda t, d, extra="": alerts.append(t))
    assert liq.get_liquidation_cooldowns("api", ME, now=NOW) == {}
    assert calls["n"] == 3                       # 3 次嘗試
    assert alerts == ["清算冷卻表建立失敗"]       # 冷快取失敗 → 告警（不是靜默 {}）


def test_cold_cache_fetch_succeeds_on_retry(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}})
    monkeypatch.setattr(liq, "_alerted", set())
    monkeypatch.setattr(liq._time, "sleep", lambda s: None)
    calls = {"n": 0}
    def flaky(api_url, payload):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("reset")
        return [{"coin": "xyz:CL", "time": int((NOW - 100) * 1000), "closedPnl": "-1",
                 "liquidation": {"liquidatedUser": ME.lower()}}]
    monkeypatch.setattr(liq, "_post", flaky)
    monkeypatch.setattr(telegram, "alert_liquidated", lambda *a: True)
    assert set(liq.get_liquidation_cooldowns("api", ME, now=NOW)) == {"xyz:CL"}


# ── W3：告警送失敗不記為已發，下次再送 ───────────────────────────
def test_failed_alert_is_retried_next_cycle(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}})
    monkeypatch.setattr(liq, "_alerted", set())
    fills = [{"coin": "xyz:CL", "time": int((NOW - 100) * 1000), "closedPnl": "-1",
              "liquidation": {"liquidatedUser": ME.lower()}}]
    monkeypatch.setattr(liq, "_post", lambda a, p: fills)
    results = iter([False, True])
    sent = []
    monkeypatch.setattr(telegram, "alert_liquidated", lambda *a: sent.append(a) or next(results))
    liq.get_liquidation_cooldowns("api", ME, now=NOW)
    liq._cache["ts"] = 0.0
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 120)
    liq._cache["ts"] = 0.0
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 240)
    assert len(sent) == 2                        # 第 1 次失敗 → 第 2 次再送 → 成功後不再送


def test_alert_liquidated_returns_send_result(monkeypatch):
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: True)
    assert telegram.alert_liquidated("xyz:CL", -1.0, "09/13 08:00") is True


# ── W1：isolated 且我方已持有部位 → 沿用持有部位的槓桿，不跟目標改 ──
def test_isolated_held_position_keeps_its_leverage(monkeypatch):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    t = Trader(None, FakeInfo(), live_trading=False)
    assert t.entry_leverage("xyz:CL", target_leverage=10, held_leverage=3) == 3
    assert t.entry_leverage("xyz:CL", target_leverage=10) == 10
    assert t.entry_leverage("BTC", target_leverage=5, held_leverage=3) == 40   # cross 不受影響


def test_safety_net_adjust_passes_held_leverage(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    monkeypatch.setattr(sync, "SIZE_TOLERANCE", 0.05)
    dry_trader._sz_dec["xyz:CL"] = 3
    seen = {}
    monkeypatch.setattr(dry_trader, "adjust_position",
                        lambda coin, cur, tgt, cs, ts, leverage, is_cross, **kw: seen.update(leverage=leverage))
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "short", "size": 1000.0, "entry_px": 100.0,
           "leverage": 10, "leverage_type": "isolated", "notional": 100000.0, "unrealized_pnl": 0.0}
    mine = {"xyz:CL": {"side": "short", "size": 1.0, "leverage": 3, "unrealized_pnl": 0.0}}
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                        {"account_value": 1000.0, "positions": mine}, scale=0.002)   # 目標 2.0 vs 我 1.0 → 加倉
    assert seen["leverage"] == 3


def test_build_desired_carries_held_leverage(dry_trader):
    dry_trader._sz_dec["xyz:CL"] = 3
    o = {"coin": "xyz:CL", "is_buy": False, "size": 1000.0, "limit_px": 100.0, "trigger_px": 0.0,
         "reduce_only": False, "is_trigger": False, "tpsl": None, "is_market": False, "tif": "Gtc",
         "order_type_name": "Limit"}
    desired, *_ = orders._build_desired(dry_trader, [o], scale=0.01, protected=set(),
                                        my_positions={"xyz:CL": {"side": "short", "size": 1.0, "leverage": 3}},
                                        target_positions={"xyz:CL": {"leverage": 10}})
    assert desired[0]["target_leverage"] == 10 and desired[0]["held_leverage"] == 3


def test_set_entry_leverage_prefers_held(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    calls = []
    monkeypatch.setattr(dry_trader, "set_leverage", lambda c, l, x: calls.append(l) or True)
    orders._set_entry_leverage(dry_trader, {"coin": "xyz:CL", "reduce_only": False, "target_leverage": 10, "held_leverage": 3})
    assert calls == [3]


# ── W2：isolated 槓桿設定失敗 → 不開倉 ───────────────────────────
def test_isolated_leverage_failure_skips_open(monkeypatch):
    ex = LevExchange({"status": "err", "response": "Cannot change leverage with open position"})
    t = Trader(ex, FakeInfo(), live_trading=True)
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    res = t.open_position("xyz:CL", False, 1.0, 4, False, entry_px=100.0)
    assert res is None and ex.opened == 0


def test_cross_leverage_failure_still_opens(monkeypatch):
    ex = LevExchange({"status": "err", "response": "boom"})
    t = Trader(ex, FakeInfo(), live_trading=True)
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "notify_open", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod, "_position_exists", lambda *a, **k: True)
    t.open_position("BTC", True, 0.001, 40, True, entry_px=100.0)
    assert ex.opened == 1                         # cross：槓桿只影響保證金，照舊行為


# ── W4：冷卻中不准反向翻倉 ────────────────────────────────────────
def test_protected_coin_blocks_reversal(monkeypatch, dry_trader):
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    called = []
    monkeypatch.setattr(dry_trader, "adjust_position", lambda *a, **k: called.append(a))
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "long", "size": 1000.0, "entry_px": 100.0,
           "leverage": 4, "leverage_type": "isolated", "notional": 100000.0, "unrealized_pnl": 0.0}
    mine = {"xyz:CL": {"side": "short", "size": 1.0, "leverage": 4, "unrealized_pnl": 0.0}}
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                        {"account_value": 1000.0, "positions": mine}, protected={"xyz:CL"}, scale=0.002)
    assert called == []
