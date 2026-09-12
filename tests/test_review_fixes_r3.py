"""Reviewer round-3 修正的回歸測試。"""
import src.telegram as telegram
from src import orders, liquidation as liq
from src import trader as trader_mod
from src.trader import Trader

ME = "0x1A1d5eF3256e1A7de2db2082D7A1eEb976c90111"
NOW = 1_800_000_000.0


class FakeInfo:
    def meta(self, dex=""):
        if dex == "xyz":
            return {"universe": [{"name": "xyz:CL", "szDecimals": 3, "maxLeverage": 20}]}
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


class GateExchange:
    """update_leverage 恆失敗；記錄 modify/cancel/order 次數。"""
    def __init__(self):
        self.lev_calls = 0; self.modifies = 0; self.cancels = 0; self.orders = 0
    def update_leverage(self, leverage, coin, is_cross):
        self.lev_calls += 1
        return {"status": "err", "response": "boom"}
    def modify_order(self, *a, **k):
        self.modifies += 1
        return {"status": "ok", "response": {"data": {"statuses": [{}]}}}
    def cancel(self, coin, oid):
        self.cancels += 1
        return {"status": "ok"}
    def order(self, *a, **k):
        self.orders += 1
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 9}}]}}}


def _spec(coin, px=100.0):
    return {"coin": coin, "is_buy": False, "size": 1.0, "limit_px": px, "trigger_px": 0.0,
            "reduce_only": False, "is_trigger": False, "tpsl": None, "is_market": False,
            "tif": "Gtc", "order_type_name": "Limit", "target_leverage": 0, "held_leverage": 0}


def _mine(coin, px, oid):
    return {"coin": coin, "is_buy": False, "reduce_only": False, "is_trigger": False,
            "limit_px": px, "trigger_px": 0.0, "size": 1.0, "oid": oid, "tpsl": None, "is_market": False}


# ── W-1：modify 路徑閘門失敗 → 取消舊單，不留 resting ─────────────────
def test_modify_gate_failure_cancels_old_order(monkeypatch):
    ex = GateExchange()
    t = Trader(ex, FakeInfo(), live_trading=True)
    t._sz_dec = {"xyz:CL": 3}
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "alert_order_sync_failed", lambda *a, **k: None)
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    monkeypatch.setattr(orders, "_modify_fail_until", {})
    res = orders._reconcile_orders(t, "api", ME, [_spec("xyz:CL", 100.0)], [_mine("xyz:CL", 101.0, 7)])
    assert ex.modifies == 0                 # 沒改單
    assert ex.cancels == 1                  # 舊單被取消
    assert ex.orders == 0                   # 重掛也被閘門擋
    assert res["cancelled"] == 1 and res["placed"] == 0


# ── W-3：isolated 槓桿失敗有退避、告警只發一次 ───────────────────────
def test_leverage_failure_backoff_limits_exchange_calls(monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(trader_mod.time, "time", lambda: clock["t"])
    ex = GateExchange()
    t = Trader(ex, FakeInfo(), live_trading=True)
    alerts = []
    monkeypatch.setattr(telegram, "alert_error", lambda et, d, extra="": alerts.append(et))
    for _ in range(3):                                          # 同一輪三張 CL 單
        assert t.prepare_entry("xyz:CL", 4, False, "掛單") is False
    assert ex.lev_calls == 1                                    # 只打一次交易所
    assert alerts == ["槓桿設定失敗"]                            # 只告警一次（prepare_entry 不再重複發）
    clock["t"] += trader_mod._LEV_FAIL_TTL + 1
    assert t.prepare_entry("xyz:CL", 4, False, "掛單") is False
    assert ex.lev_calls == 2                                    # 退避過期才再試


def test_leverage_backoff_cleared_on_success(monkeypatch):
    clock = {"t": NOW}
    monkeypatch.setattr(trader_mod.time, "time", lambda: clock["t"])
    class Flaky(GateExchange):
        def update_leverage(self, leverage, coin, is_cross):
            self.lev_calls += 1
            return {"status": "err", "response": "boom"} if self.lev_calls == 1 else {"status": "ok"}
    ex = Flaky()
    t = Trader(ex, FakeInfo(), live_trading=True)
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    assert t.set_leverage("xyz:CL", 4, False) is False
    clock["t"] += trader_mod._LEV_FAIL_TTL + 1
    assert t.set_leverage("xyz:CL", 4, False) is True
    assert t.set_leverage("xyz:CL", 4, False) is True           # 成功後走快取
    assert ex.lev_calls == 2


# ── S-1：冷／熱判斷還原為 address 相符且 ts > 0 ─────────────────────
def test_cold_cache_detected_when_ts_zero(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": ME, "until": {}, "failures": 0})
    monkeypatch.setattr(liq, "_alerted", set())
    monkeypatch.setattr(liq._time, "sleep", lambda s: None)
    def boom(a, p): raise RuntimeError("down")
    monkeypatch.setattr(liq, "_post", boom)
    alerts = []
    monkeypatch.setattr(telegram, "alert_error", lambda et, d, extra="": alerts.append(et))
    liq.get_liquidation_cooldowns("api", ME, now=NOW)
    assert alerts == ["清算冷卻表建立失敗"]                       # ts=0 視為冷快取 → 立即告警
