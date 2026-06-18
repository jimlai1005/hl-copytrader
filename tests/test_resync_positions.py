from src import orders, monitor


def test_safety_net_refetches_positions_live(monkeypatch, dry_trader):
    dry_trader.live_trading = True
    calls = {"n": 0}
    def fake_my_state(api, addr):
        calls["n"] += 1
        return {"account_value": 1000, "positions": {}}
    monkeypatch.setattr(orders, "get_my_state", fake_my_state, raising=False)
    # 隔離：sync_positions 不做事、驗證重抓掛單回空、不真的 sleep
    monkeypatch.setattr(orders, "sync_positions", lambda **k: {"actions": []})
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    target_state = {"account_value": 1000, "positions": {}, "failed_dexs": set()}
    my_state = {"account_value": 1000, "positions": {}}
    orders.sync_open_orders("api", dry_trader, target_state, my_state,
                            target_orders=[], my_orders=[], my_address="0xabc")
    assert calls["n"] == 1   # 安全網前重抓一次
