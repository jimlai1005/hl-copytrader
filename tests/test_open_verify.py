import pytest

from src import monitor
from src.trader import Trader


CONN = ConnectionError(
    "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"
)


class OpenFlakySDK:
    """market_open 一律丟暫時性錯誤；其餘呼叫成功。"""
    def __init__(self):
        self.open_calls = 0

    def update_leverage(self, *a, **k):
        return {"status": "ok"}

    def market_open(self, *a, **k):
        self.open_calls += 1
        raise CONN


class FakeInfo:
    def meta(self, dex=""):
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    from src import resilience
    monkeypatch.setattr(resilience.time, "sleep", lambda *_a, **_k: None)


def test_open_skips_resend_when_position_exists(monkeypatch):
    monkeypatch.setattr(monitor, "get_my_state",
                        lambda api, addr: {"positions": {"BTC": {"size": 0.01}},
                                           "account_value": 0.0})
    sdk = OpenFlakySDK()
    t = Trader(sdk, FakeInfo(), live_trading=True)
    t.open_position("BTC", True, 0.01, 20, True, entry_px=60000.0,
                    my_address="0xme", api_url="http://x")
    assert sdk.open_calls == 1   # 連線斷但部位已存在 → 不重送


def test_open_resends_when_position_absent(monkeypatch):
    state = {"positions": {}, "account_value": 0.0}
    monkeypatch.setattr(monitor, "get_my_state", lambda api, addr: state)
    sdk = OpenFlakySDK()
    t = Trader(sdk, FakeInfo(), live_trading=True)
    t.open_position("BTC", True, 0.01, 20, True, entry_px=60000.0,
                    my_address="0xme", api_url="http://x")
    assert sdk.open_calls == 3   # 確認沒部位 → 重送（用盡重試）
