import pytest

from src import monitor
from src.trader import Trader


CONN = ConnectionError(
    "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"
)


class OrderFlakySDK:
    """order 一律丟暫時性錯誤；其餘成功。"""
    def __init__(self):
        self.order_calls = 0

    def order(self, *a, **k):
        self.order_calls += 1
        raise CONN


class FakeInfo:
    def meta(self, dex=""):
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


def _spec(coin="BTC", is_buy=True, size=0.01, limit_px=60000.0):
    return {"coin": coin, "is_buy": is_buy, "size": size, "limit_px": limit_px,
            "trigger_px": 0.0, "reduce_only": False, "is_trigger": False,
            "tpsl": None, "is_market": False, "tif": "Gtc",
            "order_type_name": "Limit"}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    from src import resilience
    monkeypatch.setattr(resilience.time, "sleep", lambda *_a, **_k: None)


def test_place_skips_resend_when_order_rests(monkeypatch):
    resting = [{"coin": "BTC", "is_buy": True, "limit_px": 60000.0, "size": 0.01}]
    monkeypatch.setattr(monitor, "get_my_open_orders", lambda api, addr: resting)
    sdk = OrderFlakySDK()
    t = Trader(sdk, FakeInfo(), live_trading=True)
    ok, _ = t.place_order(_spec(), my_address="0xme", api_url="http://x")
    assert sdk.order_calls == 1   # 連線斷但掛單已在 → 不重送
    assert ok is True


def test_place_resends_when_order_absent(monkeypatch):
    monkeypatch.setattr(monitor, "get_my_open_orders", lambda api, addr: [])
    sdk = OrderFlakySDK()
    t = Trader(sdk, FakeInfo(), live_trading=True)
    t.place_order(_spec(), my_address="0xme", api_url="http://x")
    assert sdk.order_calls == 3   # 確認沒這張 → 重送（用盡重試）
