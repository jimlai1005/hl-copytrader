"""
Characterization tests for the LIVE order/position execution paths of Trader.

These lock down CURRENT behavior (which SDK methods get called with which args,
and how order errors are routed to Telegram) so the upcoming refactors
(P2-3/P2-4) cannot silently change behavior. They use a FakeExchange that records
calls and a captured-telegram fixture. They are NOT testing new behavior — if any
assertion here fails after a refactor, the refactor changed observable behavior.
"""
import pytest

from src.trader import Trader


# ── Fakes ────────────────────────────────────────────────────────────────
class FakeExchange:
    """Records every SDK call; returns a configurable result per method."""
    OK = {"status": "ok", "response": {"data": {"statuses": [{}]}}}

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    def _record(self, name, args, kwargs):
        self.calls.append((name, args, kwargs))
        return self.results.get(name, self.OK)

    def order(self, *a, **k): return self._record("order", a, k)
    def market_open(self, *a, **k): return self._record("market_open", a, k)
    def market_close(self, *a, **k): return self._record("market_close", a, k)
    def modify_order(self, *a, **k): return self._record("modify_order", a, k)
    def cancel(self, *a, **k): return self._record("cancel", a, k)
    def update_leverage(self, *a, **k): return self._record("update_leverage", a, k)


class FakeInfo:
    def meta(self, dex=""):
        if dex == "xyz":
            return {"universe": [{"name": "xyz:NVDA", "szDecimals": 2,
                                  "maxLeverage": 20, "onlyIsolated": True}]}
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


def _err_result(msg):
    return {"status": "ok", "response": {"data": {"statuses": [{"error": msg}]}}}


def _call_names(ex):
    return [c[0] for c in ex.calls]


def _last_call(ex, name):
    for c in reversed(ex.calls):
        if c[0] == name:
            return c
    return None


# ── Fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture
def captured_tg(monkeypatch):
    """Replace the tg notify/alert functions with recorders (bypasses gating)."""
    from src import telegram
    names = [
        "notify_order_placed", "notify_order_modified", "notify_open", "notify_close",
        "alert_insufficient_balance", "alert_api_error", "alert_error",
    ]
    rec = {n: [] for n in names}
    for n in names:
        monkeypatch.setattr(telegram, n, (lambda nn: (lambda *a, **k: rec[nn].append((a, k))))(n))
    return rec


def _live_trader(ex):
    t = Trader(ex, FakeInfo(), live_trading=True)
    return t


def _spec(coin="BTC", is_buy=True, size=0.01, limit_px=60000.0, trigger_px=0.0,
          reduce_only=False, is_trigger=False, tpsl=None, is_market=False,
          tif="Gtc", order_type_name="Limit"):
    return {"coin": coin, "is_buy": is_buy, "size": size, "limit_px": limit_px,
            "trigger_px": trigger_px, "reduce_only": reduce_only,
            "is_trigger": is_trigger, "tpsl": tpsl, "is_market": is_market,
            "tif": tif, "order_type_name": order_type_name}


# ── place_order ──────────────────────────────────────────────────────────
def test_place_order_limit_success(captured_tg):
    ex = FakeExchange()
    ok, _res = _live_trader(ex).place_order(_spec())
    assert ok is True
    name, args, kwargs = _last_call(ex, "order")
    assert args == ("BTC", True, 0.01, 60000.0)
    assert kwargs["order_type"] == {"limit": {"tif": "Gtc"}}
    assert kwargs["reduce_only"] is False
    assert len(captured_tg["notify_order_placed"]) == 1


def test_place_order_trigger_uses_trigger_order_type(captured_tg):
    ex = FakeExchange()
    spec = _spec(is_trigger=True, trigger_px=58000.0, tpsl="sl", is_market=True,
                 order_type_name="Stop Market")
    _live_trader(ex).place_order(spec)
    _name, _args, kwargs = _last_call(ex, "order")
    assert kwargs["order_type"] == {"trigger": {"triggerPx": 58000.0,
                                                "isMarket": True, "tpsl": "sl"}}


def test_place_order_spot_skipped(captured_tg):
    ex = FakeExchange()
    ok, res = _live_trader(ex).place_order(_spec(coin="@85"))
    assert ok is False and res is None
    assert ex.calls == []


def test_place_order_insufficient_routes_balance_alert(captured_tg):
    ex = FakeExchange(results={"order": _err_result("Insufficient margin to place order")})
    ok, _res = _live_trader(ex).place_order(_spec())
    assert ok is False
    assert len(captured_tg["alert_insufficient_balance"]) == 1
    assert len(captured_tg["notify_order_placed"]) == 0


def test_place_order_market_closed_routes_error_alert(captured_tg):
    ex = FakeExchange(results={"order": _err_result("Market is closed for this asset")})
    _live_trader(ex).place_order(_spec(coin="xyz:NVDA", limit_px=200.0))
    assert len(captured_tg["alert_error"]) == 1


def test_place_order_other_error_routes_api_alert(captured_tg):
    ex = FakeExchange(results={"order": _err_result("Order has invalid size")})
    _live_trader(ex).place_order(_spec())
    assert len(captured_tg["alert_api_error"]) == 1


# ── modify_order ─────────────────────────────────────────────────────────
def test_modify_order_success(captured_tg):
    ex = FakeExchange()
    ok = _live_trader(ex).modify_order(123, _spec(limit_px=59000.0))
    assert ok is True
    name, args, kwargs = _last_call(ex, "modify_order")
    assert args[0] == 123 and args[1] == "BTC"
    assert kwargs["reduce_only"] is False
    assert len(captured_tg["notify_order_modified"]) == 1


def test_modify_order_error_returns_false_no_alert(captured_tg):
    ex = FakeExchange(results={"modify_order": _err_result("Insufficient margin")})
    ok = _live_trader(ex).modify_order(123, _spec())
    assert ok is False
    # modify failure stays silent (reconcile handles the fallback)
    assert len(captured_tg["alert_api_error"]) == 0
    assert len(captured_tg["notify_order_modified"]) == 0


# ── open_position ────────────────────────────────────────────────────────
def test_open_position_default_dex(captured_tg):
    ex = FakeExchange()
    _live_trader(ex).open_position("BTC", True, 0.01, 40, True, entry_px=60000.0)
    assert "update_leverage" in _call_names(ex)
    name, args, kwargs = _last_call(ex, "market_open")
    assert args == ("BTC", True, 0.01)   # no px for default dex
    assert "px" not in kwargs
    assert len(captured_tg["notify_open"]) == 1


def test_open_position_xyz_passes_px(captured_tg):
    ex = FakeExchange()
    _live_trader(ex).open_position("xyz:NVDA", True, 1.0, 20, False, entry_px=200.0)
    name, args, kwargs = _last_call(ex, "market_open")
    assert args[0] == "xyz:NVDA"
    assert kwargs.get("px") == 200.0   # xyz must pass mid price


def test_open_position_spot_skipped(captured_tg):
    ex = FakeExchange()
    res = _live_trader(ex).open_position("@85", True, 1.0, 10, True, entry_px=5.0)
    assert res is None
    assert ex.calls == []


# ── close_position ───────────────────────────────────────────────────────
def test_close_position_default_uses_market_close(captured_tg):
    ex = FakeExchange()
    _live_trader(ex).close_position("BTC", True, 0.01)
    name, args, kwargs = _last_call(ex, "market_close")
    assert args == ("BTC", 0.01)
    assert len(captured_tg["notify_close"]) == 1


def test_close_position_xyz_uses_reduce_only_ioc_order(captured_tg, monkeypatch):
    from src import monitor
    monkeypatch.setattr(monitor, "get_mid_price", lambda api, coin: 200.0)
    ex = FakeExchange()
    # is_buy=True means we hold long → close sells (close_is_buy=False)
    _live_trader(ex).close_position("xyz:NVDA", True, 1.0, api_url="api")
    name, args, kwargs = _last_call(ex, "order")
    assert args[0] == "xyz:NVDA"
    assert args[1] is False                       # close a long → sell
    assert kwargs["order_type"] == {"limit": {"tif": "Ioc"}}
    assert kwargs["reduce_only"] is True


def test_close_position_none_result_no_notify(captured_tg):
    ex = FakeExchange(results={"market_close": None})
    res = _live_trader(ex).close_position("BTC", True, 0.01)
    assert res is None
    assert len(captured_tg["notify_close"]) == 0


# ── set_leverage / cancel_one ────────────────────────────────────────────
def test_set_leverage_err_status_alerts_and_false(captured_tg):
    ex = FakeExchange(results={"update_leverage": {"status": "err",
                                                   "response": "Cross margin is not allowed"}})
    ok = _live_trader(ex).set_leverage("xyz:NVDA", 20, True)
    assert ok is False
    assert len(captured_tg["alert_error"]) == 1


def test_cancel_one_calls_exchange(captured_tg):
    ex = FakeExchange()
    ok = _live_trader(ex).cancel_one("BTC", 999)
    assert ok is True
    assert _last_call(ex, "cancel")[1] == ("BTC", 999)
