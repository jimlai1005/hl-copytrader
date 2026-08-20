"""Task 2（z 只用已完成日）迴歸測試：

_daily_abs_pnl 剔除「進行中的今日(UTC)」日桶；今日無樣本時最後完整日不被誤丟；
部分日數值完全不影響統計（日內恆定）；剔除後資料不足回 None。
"""
from datetime import datetime, timezone

from src import weight


class _FakeDT(datetime):
    """datetime 替身：now() 固定在 2026-08-20 12:00 UTC，其餘行為不變。"""
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _ts(y, m, d, h=8):
    return int(datetime(y, m, d, h, tzinfo=timezone.utc).timestamp() * 1000)


def _payload(hist):
    return [["month", {"pnlHistory": hist}]]


BASE_HIST = [
    [_ts(2026, 8, 14), 0],
    [_ts(2026, 8, 15), 100],
    [_ts(2026, 8, 16), 250],
    [_ts(2026, 8, 17), 130],
    [_ts(2026, 8, 18), 180],
    [_ts(2026, 8, 19), 300],
]
# 完整日 |PnL| 序列 = [100, 150, 120, 50, 120]（8/15~8/19）


def test_partial_today_excluded(monkeypatch):
    """今日(8/20)的部分日桶被剔除，其數值不進入序列。"""
    monkeypatch.setattr(weight, "datetime", _FakeDT)
    hist = BASE_HIST + [[_ts(2026, 8, 20), 99999]]
    monkeypatch.setattr(weight, "_post", lambda url, body: _payload(hist))
    assert weight._daily_abs_pnl("0xabc") == [100, 150, 120, 50, 120]


def test_no_sample_today_keeps_last_completed(monkeypatch):
    """今日無樣本 → 最後一個完整日(8/19)不被誤丟（防 off-by-one）。"""
    monkeypatch.setattr(weight, "datetime", _FakeDT)
    monkeypatch.setattr(weight, "_post", lambda url, body: _payload(BASE_HIST))
    assert weight._daily_abs_pnl("0xabc") == [100, 150, 120, 50, 120]


def test_intraday_constancy(monkeypatch):
    """同一 UTC 日內，部分日 |PnL| 怎麼變都不影響統計（日內恆定）。"""
    monkeypatch.setattr(weight, "datetime", _FakeDT)
    out = []
    for todays_cum in (301, 99999):
        hist = BASE_HIST + [[_ts(2026, 8, 20), todays_cum]]
        monkeypatch.setattr(weight, "_post", lambda url, body, h=hist: _payload(h))
        out.append(weight.compute_volatility_stats("0xabc"))
    assert out[0] is not None
    assert out[0] == out[1]


def test_insufficient_after_drop_returns_none(monkeypatch):
    """剔除部分日後不足 3 個完整日 → 回 None（weight 回 1，安全預設）。"""
    monkeypatch.setattr(weight, "datetime", _FakeDT)
    hist = [[_ts(2026, 8, 19), 100], [_ts(2026, 8, 20), 250]]
    monkeypatch.setattr(weight, "_post", lambda url, body: _payload(hist))
    assert weight.compute_volatility_stats("0xabc") is None
