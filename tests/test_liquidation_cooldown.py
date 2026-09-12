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
    liq._cache["ts"] = 0.0                      # 讓快取失效，強迫重抓
    again = liq.get_liquidation_cooldowns("api", ME, now=NOW + 120)
    assert again == first                       # 失敗 → 沿用上次結果，不是空集合


def test_new_liquidation_alerts_once(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 100, ME, pnl="-9.52")])
    sent = []
    from src import telegram
    monkeypatch.setattr(telegram, "alert_liquidated", lambda coin, pnl, until: sent.append((coin, pnl)))
    liq.get_liquidation_cooldowns("api", ME, now=NOW)
    liq._cache["ts"] = 0.0
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 120)   # 同一筆再看到 → 不重發
    assert sent == [("xyz:CL", -9.52)]
