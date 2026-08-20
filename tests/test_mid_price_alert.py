"""5.1（取價失敗告警）迴歸測試：

sync 迴圈中 get_mid_price 失敗 → 發 Telegram 告警（dedup key 帶 coin），
控制流與現況完全一致（有 entry_px 後備 → 照常同步；無後備 → 本輪跳過該標的）。
"""
from unittest.mock import MagicMock

import pytest

from src import sync
from tests.conftest import make_pos


def _states(entry_px):
    target_state = {
        "account_value": 1000.0,
        "failed_dexs": set(),
        "positions": {"BTC": make_pos("BTC", side="long", size=1.0,
                                      notional=100.0, entry_px=entry_px)},
    }
    my_state = {"account_value": 100.0, "positions": {}}
    return target_state, my_state


def test_alert_fired_and_fallback_price_used(dry_trader, monkeypatch):
    """取價失敗但有 entry_px 後備 → 告警一次，且照常用後備價同步（行為不變）。"""
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: None)
    alert = MagicMock()
    monkeypatch.setattr(sync.tg, "alert_mid_price_failed", alert)

    target_state, my_state = _states(entry_px=100.0)
    # scale=0.5 → 目標 size 0.5 × $100 = $50 ≥ $10 → 會走到開倉（dry_run）
    r = sync.sync_positions("http://x", dry_trader, target_state, my_state, scale=0.5)

    alert.assert_called_once()
    assert alert.call_args[0][0] == "BTC"
    opens = [a for a in r["actions"] if a["action"] == "open"]
    assert len(opens) == 1 and opens[0]["entry_px"] == 100.0   # 後備價被沿用


def test_alert_fired_and_coin_skipped_when_no_fallback(dry_trader, monkeypatch):
    """取價失敗且無後備價（entry_px=0）→ 告警一次，該標的本輪被跳過（行為不變）。"""
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: None)
    alert = MagicMock()
    monkeypatch.setattr(sync.tg, "alert_mid_price_failed", alert)

    target_state, my_state = _states(entry_px=0.0)
    r = sync.sync_positions("http://x", dry_trader, target_state, my_state, scale=0.5)

    alert.assert_called_once()
    assert r["actions"] == []   # notional=0 < $10 → continue（既有行為）


def test_no_alert_when_price_ok(dry_trader, monkeypatch):
    """取價正常 → 不告警（不誤報）。"""
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    alert = MagicMock()
    monkeypatch.setattr(sync.tg, "alert_mid_price_failed", alert)

    target_state, my_state = _states(entry_px=100.0)
    sync.sync_positions("http://x", dry_trader, target_state, my_state, scale=0.5)

    alert.assert_not_called()


def test_dedup_key_carries_coin(monkeypatch):
    """告警函式的 dedup key 帶 coin：同 coin 連續失敗由既有 _send TTL 去重，
    不同 coin 各自獨立（API 整段故障時不會打爆 Telegram）。"""
    from src import telegram
    keys = []
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: keys.append(k.get("dedup_key")))
    telegram.alert_mid_price_failed("BTC", "x")
    telegram.alert_mid_price_failed("BTC", "x")
    telegram.alert_mid_price_failed("ETH", "x")
    assert keys == ["mid_px_failed:BTC", "mid_px_failed:BTC", "mid_px_failed:ETH"]
