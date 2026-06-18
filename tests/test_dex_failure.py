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
