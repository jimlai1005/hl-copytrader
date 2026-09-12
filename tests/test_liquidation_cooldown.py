"""清算冷卻：從 userFillsByTime 的 liquidation.liquidatedUser 欄位偵測我方被清算，
該標的進入 LIQUIDATION_COOLDOWN_HOURS 冷卻（只准減倉、不新開不加倉、不掛補倉單）。

為何：安全網無記憶，被清算後看到「目標有、我沒有」1 分鐘內就市價重開，30 天內
「清算後 5 分鐘內重開」23 筆，CL 在 6 小時內被清算兩次。"""
import time

from src import liquidation as liq

ME = "0x1A1d5eF3256e1A7de2db2082D7A1eEb976c90111"
NOW = 1_800_000_000.0   # 固定「現在」(秒)


def _fill(coin, t_sec, liquidated_user=None, pnl="-5.0"):
    f = {"coin": coin, "time": int(t_sec * 1000), "closedPnl": pnl, "side": "B", "sz": "1", "px": "100"}
    if liquidated_user:
        f["liquidation"] = {"liquidatedUser": liquidated_user, "markPx": "100", "method": "market"}
    return f


def _install(monkeypatch, fills, hours=2.0):
    calls = {"n": 0}
    def post(api_url, payload):
        calls["n"] += 1
        assert payload["type"] == "userFillsByTime"
        assert payload["user"] == ME
        return fills
    monkeypatch.setattr(liq, "_post", post)
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", hours)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}})
    monkeypatch.setattr(liq, "_alerted", set())      # 模組級集合，測試間必須重置
    return calls


def test_my_liquidation_enters_cooldown(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 1800, ME.lower())])
    out = liq.get_liquidation_cooldowns("api", ME, now=NOW)
    assert set(out) == {"xyz:CL"}
    assert out["xyz:CL"] == (NOW - 1800) + 2 * 3600


def test_counterparty_liquidation_is_ignored(monkeypatch):
    _install(monkeypatch, [_fill("HYPE", NOW - 60, "0x000000000000000000000000000000000000dead")])
    assert liq.get_liquidation_cooldowns("api", ME, now=NOW) == {}


def test_normal_fill_is_ignored(monkeypatch):
    _install(monkeypatch, [_fill("HYPE", NOW - 60)])
    assert liq.get_liquidation_cooldowns("api", ME, now=NOW) == {}


def test_expired_cooldown_dropped(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 3 * 3600, ME)])
    assert liq.get_liquidation_cooldowns("api", ME, now=NOW) == {}


def test_latest_liquidation_wins(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 5000, ME), _fill("xyz:CL", NOW - 100, ME)])
    out = liq.get_liquidation_cooldowns("api", ME, now=NOW)
    assert out["xyz:CL"] == (NOW - 100) + 2 * 3600


def test_cached_within_ttl(monkeypatch):
    calls = _install(monkeypatch, [_fill("xyz:CL", NOW - 100, ME)])
    liq.get_liquidation_cooldowns("api", ME, now=NOW)
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 10)
    assert calls["n"] == 1


def test_fetch_failure_keeps_previous_flags(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 100, ME)])
    first = liq.get_liquidation_cooldowns("api", ME, now=NOW)
    def boom(api_url, payload):
        raise RuntimeError("net down")
    monkeypatch.setattr(liq, "_post", boom)
    again = liq.get_liquidation_cooldowns("api", ME, now=NOW + 120)
    assert again == first                       # 失敗 → 沿用上次結果，不是空集合


def test_new_liquidation_alerts_once(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 100, ME, pnl="-9.52")])
    sent = []
    from src import telegram
    monkeypatch.setattr(telegram, "alert_liquidated", lambda coin, pnl, until: sent.append((coin, pnl)) or True)
    liq.get_liquidation_cooldowns("api", ME, now=NOW)
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 120)   # 同一筆再看到 → 不重發
    assert sent == [("xyz:CL", -9.52)]


# 追加到 tests/test_liquidation_cooldown.py 末尾
from src import orders, sync


def _tgt_order(coin, reduce_only=False):
    return {"coin": coin, "is_buy": True, "size": 1000.0, "limit_px": 100.0, "trigger_px": 0.0,
            "reduce_only": reduce_only, "is_trigger": False, "tpsl": None,
            "is_market": False, "tif": "Gtc", "order_type_name": "Limit"}


def test_sync_open_orders_blocks_cooling_coin_orders(monkeypatch, dry_trader):
    dry_trader._sz_dec["xyz:CL"] = 3
    monkeypatch.setattr(orders, "get_liquidation_cooldowns", lambda api, addr: {"xyz:CL": NOW + 3600})
    monkeypatch.setattr(orders, "sync_positions", lambda **k: {"actions": []})
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    seen = {}
    def fake_reconcile(trader, api_url, my_address, desired, my_orders):
        seen["coins"] = [(d["coin"], d["reduce_only"]) for d in desired]
        return {"placed": 0, "cancelled": 0, "modified": 0, "matched": 0, "sync_failed": False}
    monkeypatch.setattr(orders, "_reconcile_orders", fake_reconcile)
    target_state = {"account_value": 1000.0, "positions": {}, "failed_dexs": set()}
    my_state = {"account_value": 1000.0, "positions": {"xyz:CL": {"side": "short", "size": 1.0}}}
    orders.sync_open_orders("api", dry_trader, target_state, my_state,
                            target_orders=[_tgt_order("xyz:CL"), _tgt_order("xyz:CL", reduce_only=True), _tgt_order("BTC")],
                            my_orders=[], my_address=ME)
    # 冷卻中的 CL：補倉單被擋、reduce-only 保留；BTC 不受影響
    assert seen["coins"] == [("xyz:CL", True), ("BTC", False)]   # C3：留下的 CL 必須是 reduce-only


def test_sync_open_orders_passes_cooldown_to_safety_net(monkeypatch, dry_trader):
    monkeypatch.setattr(orders, "get_liquidation_cooldowns", lambda api, addr: {"xyz:CL": NOW + 3600})
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    monkeypatch.setattr(orders, "_reconcile_orders",
                        lambda *a, **k: {"placed": 0, "cancelled": 0, "modified": 0, "matched": 0, "sync_failed": False})
    got = {}
    monkeypatch.setattr(orders, "sync_positions", lambda **k: got.update(protected=k["protected"]) or {"actions": []})
    target_state = {"account_value": 1000.0, "positions": {}, "failed_dexs": set()}
    orders.sync_open_orders("api", dry_trader, target_state, {"account_value": 1000.0, "positions": {}},
                            target_orders=[], my_orders=[], my_address=ME)
    assert got["protected"] == {"xyz:CL"}


def test_dry_run_without_address_skips_cooldown_fetch(monkeypatch, dry_trader):
    called = {"n": 0}
    monkeypatch.setattr(orders, "get_liquidation_cooldowns", lambda api, addr: called.__setitem__("n", called["n"] + 1) or {})
    monkeypatch.setattr(orders, "sync_positions", lambda **k: {"actions": []})
    monkeypatch.setattr(orders, "_reconcile_orders",
                        lambda *a, **k: {"placed": 0, "cancelled": 0, "modified": 0, "matched": 0, "sync_failed": False})
    target_state = {"account_value": 1000.0, "positions": {}, "failed_dexs": set()}
    orders.sync_open_orders("api", dry_trader, target_state, {"account_value": 1000.0, "positions": {}},
                            target_orders=[], my_orders=[], my_address="")
    assert called["n"] == 0


def test_safety_net_cooling_coin_no_reopen_but_close_allowed(monkeypatch, dry_trader):
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    opened = []; closed = []
    monkeypatch.setattr(dry_trader, "open_position", lambda *a, **k: opened.append(a) or {"status": "dry_run"})
    monkeypatch.setattr(dry_trader, "close_position", lambda *a, **k: closed.append(a) or {"status": "dry_run"})
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "short", "size": 500.0, "entry_px": 100.0,
           "leverage": 4, "leverage_type": "isolated", "notional": 50000.0, "unrealized_pnl": 0.0}
    # 情境 1：目標有、我沒有、CL 冷卻中 → 不開
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                        {"account_value": 1000.0, "positions": {}}, protected={"xyz:CL"}, scale=0.002)
    assert opened == []
    # 情境 2：目標已平、我還有、CL 冷卻中 → 照平（冷卻只擋開/加倉）
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {}},
                        {"account_value": 1000.0, "positions": {"xyz:CL": {"side": "short", "size": 1.0, "unrealized_pnl": 0.0}}},
                        protected={"xyz:CL"}, scale=0.002)
    assert len(closed) == 1
