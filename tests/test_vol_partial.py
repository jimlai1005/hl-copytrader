"""波動統計用『現有資料』計算：只有 6 天就用 6 天（基準 5 天）算 μ/σ/Z，
天數少時較不準但仍據實計算、不藏起來；< 3 天才回 None。"""
from src import weight


def test_uses_available_days(monkeypatch):
    # 6 天 → 基準取前 5 天，第 6 天為 today
    monkeypatch.setattr(weight, "_daily_abs_pnl", lambda a: [10, 12, 8, 14, 6, 30])
    s = weight.compute_volatility_stats("0xabc")
    assert s["days"] == 5
    assert s["today"] == 30
    assert "z" in s and "mu" in s and "sigma" in s
    assert "ready" not in s          # 不再有暖機旗標


def test_caps_baseline_at_lookback(monkeypatch):
    # 20 天 → 基準只取前 LOOKBACK_DAYS 天
    monkeypatch.setattr(weight, "_daily_abs_pnl", lambda a: [float(i) for i in range(20)])
    s = weight.compute_volatility_stats("0xabc")
    assert s["days"] == weight.LOOKBACK_DAYS


def test_too_few_days_is_none(monkeypatch):
    monkeypatch.setattr(weight, "_daily_abs_pnl", lambda a: [10, 12])  # 只有 2 天
    assert weight.compute_volatility_stats("0xabc") is None


def test_abnormal_today_lowers_weight(monkeypatch):
    # 今天異常大 → Z 高 → 權重 < 1（自動去槓桿）
    monkeypatch.setattr(weight, "_daily_abs_pnl", lambda a: [8, 10, 12, 9, 100])
    s = weight.compute_volatility_stats("0xabc")
    assert s["z"] > 0
    assert s["weight"] < 1.0


def test_position_weight_applies(monkeypatch):
    monkeypatch.setattr(weight, "VOLATILITY_WEIGHT_ENABLED", True)
    monkeypatch.setattr(weight, "POSITION_WEIGHT", 1.0)
    monkeypatch.setattr(weight, "get_vol_stats", lambda a: {"weight": 0.5})
    assert weight.get_position_weight() == 0.5   # 1.0 × 0.5
