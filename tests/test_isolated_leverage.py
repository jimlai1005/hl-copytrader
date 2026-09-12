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


# ── 呼叫端接線 ──────────────────────────────────────────────
from src import sync, orders


def _tgt_pos(coin, size, leverage, lev_type="isolated", side="long"):
    return {"coin": coin, "dex": "xyz" if ":" in coin else "", "side": side, "size": size,
            "entry_px": 100.0, "leverage": leverage, "leverage_type": lev_type,
            "notional": size * 100.0, "unrealized_pnl": 0.0}


def test_safety_net_open_passes_target_leverage(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    seen = {}
    def fake_open(coin, is_buy, size, leverage, is_cross, **kw):
        seen.update(coin=coin, leverage=leverage, is_cross=is_cross)
        return {"status": "dry_run"}
    monkeypatch.setattr(dry_trader, "open_position", fake_open)
    target_state = {"account_value": 1000.0, "failed_dexs": set(),
                    "positions": {"xyz:CL": _tgt_pos("xyz:CL", 500.0, 4, side="short")}}
    my_state = {"account_value": 1000.0, "positions": {}}
    sync.sync_positions("api", dry_trader, target_state, my_state, scale=0.002)
    assert seen == {"coin": "xyz:CL", "leverage": 4, "is_cross": False}


def test_safety_net_open_uses_target_leverage_3(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:MINIMAX"] = 2
    seen = {}
    monkeypatch.setattr(dry_trader, "open_position",
                        lambda coin, is_buy, size, leverage, is_cross, **kw: seen.update(leverage=leverage) or {"status": "dry_run"})
    target_state = {"account_value": 1000.0, "failed_dexs": set(),
                    "positions": {"xyz:MINIMAX": _tgt_pos("xyz:MINIMAX", 2700.0, 3)}}
    sync.sync_positions("api", dry_trader, target_state, {"account_value": 1000.0, "positions": {}}, scale=0.001)
    assert seen["leverage"] == 3


def _tgt_order(coin, size=1000.0, px=100.0, reduce_only=False, is_buy=True):
    return {"coin": coin, "is_buy": is_buy, "size": size, "limit_px": px, "trigger_px": 0.0,
            "reduce_only": reduce_only, "is_trigger": False, "tpsl": None,
            "is_market": False, "tif": "Gtc", "order_type_name": "Limit"}


def test_build_desired_carries_target_leverage(dry_trader):
    dry_trader._sz_dec["xyz:CL"] = 3
    target_positions = {"xyz:CL": _tgt_pos("xyz:CL", 500.0, 4, side="short")}
    desired, *_ = orders._build_desired(
        dry_trader, [_tgt_order("xyz:CL"), _tgt_order("BTC")], scale=0.01,
        protected=set(), my_positions={}, target_positions=target_positions,
    )
    by_coin = {d["coin"]: d for d in desired}
    assert by_coin["xyz:CL"]["target_leverage"] == 4
    assert by_coin["BTC"]["target_leverage"] == 0       # 目標無部位 → 0


def test_set_entry_leverage_uses_spec_target_leverage(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    calls = []
    monkeypatch.setattr(dry_trader, "set_leverage", lambda coin, lev, cross: calls.append((coin, lev, cross)) or True)
    orders._set_entry_leverage(dry_trader, {"coin": "xyz:CL", "reduce_only": False, "target_leverage": 6})
    orders._set_entry_leverage(dry_trader, {"coin": "xyz:CL", "reduce_only": False, "target_leverage": 0})
    orders._set_entry_leverage(dry_trader, {"coin": "xyz:CL", "reduce_only": False})   # 舊 spec 無欄位
    assert calls == [("xyz:CL", 6, False), ("xyz:CL", 4, False), ("xyz:CL", 4, False)]
