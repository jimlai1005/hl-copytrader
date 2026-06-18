import pytest
from src import monitor


def _fake_post_xyz_fails(api_url, payload):
    if payload.get("dex") == "xyz":
        raise RuntimeError("network blip")
    if payload["type"] == "clearinghouseState":
        return {"marginSummary": {"accountValue": "1000", "totalRawUsd": "1000"},
                "assetPositions": []}
    return {}


def test_get_trader_state_marks_failed_dex(monkeypatch):
    monkeypatch.setattr(monitor, "EXTRA_DEXS", ["xyz"])
    monkeypatch.setattr(monitor, "_post", _fake_post_xyz_fails)
    state = monitor.get_trader_state("api", "0xabc")
    assert state["failed_dexs"] == {"xyz"}
    assert state["account_value"] == 1000.0


def test_get_trader_state_no_failure(monkeypatch):
    def ok_post(api_url, payload):
        return {"marginSummary": {"accountValue": "500", "totalRawUsd": "500"},
                "assetPositions": []}
    monkeypatch.setattr(monitor, "EXTRA_DEXS", ["xyz"])
    monkeypatch.setattr(monitor, "_post", ok_post)
    state = monitor.get_trader_state("api", "0xabc")
    assert state["failed_dexs"] == set()


from src import sync
from tests.conftest import make_pos


def test_safety_net_skips_failed_dex_close(dry_trader):
    target_state = {"account_value": 1000, "positions": {}, "failed_dexs": {"xyz"}}
    my_state = {"account_value": 1000, "positions": {
        "xyz:NVDA": make_pos("xyz:NVDA", lev_type="isolated"),
        "BTC": make_pos("BTC", size=0.01, notional=600, entry_px=60000),
    }}
    result = sync.sync_positions("api", dry_trader, target_state, my_state)
    closed = [a["coin"] for a in result["actions"] if a["action"] == "close"]
    assert "BTC" in closed
    assert "xyz:NVDA" not in closed
