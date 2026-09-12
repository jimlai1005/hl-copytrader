"""isolated 標的的名目槓桿：跟目標槓桿；目標無部位時用 ISOLATED_ORDER_LEVERAGE。
cross 標的維持既有行為（ORDER_LEVERAGE=max → 標的最大槓桿）。

為何：isolated 部位的保證金＝名目/槓桿，槓桿直接決定清算價；
一律 max 讓 xyz 標的在 2.5%~5% 反向走勢就被清算（30 天 20 次），目標自己用 3~4x。"""
import pytest

from src import trader as trader_mod
from src.trader import Trader


class FakeInfo:
    def meta(self, dex=""):
        if dex == "xyz":
            return {"universe": [
                {"name": "xyz:CL", "szDecimals": 3, "maxLeverage": 20},
                {"name": "xyz:MINIMAX", "szDecimals": 2, "maxLeverage": 10, "onlyIsolated": True},
            ]}
        return {"universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "PONS", "szDecimals": 0, "maxLeverage": 3, "onlyIsolated": True},
        ]}


@pytest.fixture
def t(monkeypatch):
    monkeypatch.setattr(trader_mod, "ORDER_LEVERAGE", "max")
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    return Trader(None, FakeInfo(), live_trading=False)


def test_cross_keeps_max(t):
    assert t.entry_leverage("BTC") == 40


def test_cross_ignores_target_leverage(t):
    assert t.entry_leverage("BTC", target_leverage=5) == 40


def test_isolated_default_when_no_target_position(t):
    assert t.entry_leverage("xyz:CL") == 4


def test_isolated_follows_target_leverage(t):
    assert t.entry_leverage("xyz:CL", target_leverage=3) == 3


def test_isolated_target_leverage_clamped_to_max(t):
    assert t.entry_leverage("xyz:MINIMAX", target_leverage=50) == 10


def test_isolated_default_clamped_to_max(t, monkeypatch):
    # PONS 上限 3x < 預設 4x → 夾到 3
    assert t.entry_leverage("PONS") == 3


def test_only_isolated_on_default_dex_uses_isolated_rule(t):
    # PONS 是預設 dex 但 onlyIsolated → 走 isolated 規則（跟目標）
    assert t.entry_leverage("PONS", target_leverage=2) == 2


def test_isolated_without_info_uses_default(monkeypatch):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    t = Trader(None, None, live_trading=False)
    assert t.entry_leverage("xyz:CL") == 4          # 無 info：xyz 靠前綴判 isolated
    assert t.entry_leverage("xyz:CL", target_leverage=6) == 6
