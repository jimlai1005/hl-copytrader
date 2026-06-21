"""波動權重暖機：歷史不足時回 ready=False、權重維持 1.0（不影響交易）；
歷史足夠才 ready=True 算 Z-Score。修正「暖機中靜默、像壞掉」的 UX 問題。"""
from src import weight


def test_vol_stats_warmup_returns_dict(monkeypatch):
    monkeypatch.setattr(weight, "_daily_abs_pnl", lambda a: [1.0] * 6)  # 6 天 < 15
    s = weight.compute_volatility_stats("0xabc")
    assert s["ready"] is False
    assert s["days"] == 6
    assert s["needed"] == weight.LOOKBACK_DAYS + 1
    assert s["weight"] == 1.0


def test_vol_stats_empty_is_none(monkeypatch):
    monkeypatch.setattr(weight, "_daily_abs_pnl", lambda a: [])
    assert weight.compute_volatility_stats("0xabc") is None


def test_vol_stats_ready_when_enough(monkeypatch):
    monkeypatch.setattr(weight, "_daily_abs_pnl", lambda a: [10.0] * 15)  # today==baseline
    s = weight.compute_volatility_stats("0xabc")
    assert s["ready"] is True
    assert s["weight"] == 1.0   # today == mu → sigma=0 → 權重 1.0


def test_position_weight_ignores_warmup(monkeypatch):
    # 暖機中即使波動權重 0.3 也不該套用，交易維持手動權重（不去槓桿）
    monkeypatch.setattr(weight, "VOLATILITY_WEIGHT_ENABLED", True)
    monkeypatch.setattr(weight, "POSITION_WEIGHT", 1.0)
    monkeypatch.setattr(weight, "get_vol_stats",
                        lambda a: {"ready": False, "days": 6, "needed": 15, "weight": 0.3})
    assert weight.get_position_weight() == 1.0


def test_position_weight_applies_when_ready(monkeypatch):
    monkeypatch.setattr(weight, "VOLATILITY_WEIGHT_ENABLED", True)
    monkeypatch.setattr(weight, "POSITION_WEIGHT", 1.0)
    monkeypatch.setattr(weight, "get_vol_stats", lambda a: {"ready": True, "weight": 0.5})
    assert weight.get_position_weight() == 0.5   # 1.0 × 0.5
