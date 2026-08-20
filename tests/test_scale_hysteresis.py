"""Task 1（scale 遲滯）迴歸測試：

get_stable_scale 的採用規則 adopt ⟺ applied is None or |raw−applied| >= HYST×applied，
以及 sync_positions 的單一計算點（scale 參數傳入時不重算）。
"""
from unittest.mock import MagicMock

import pytest

from src import sync


def _feed(monkeypatch, raws, hyst=0.10):
    """讓 compute_scale_factor 依序回傳 raws，並固定遲滯帶寬。"""
    it = iter(raws)
    monkeypatch.setattr(sync, "compute_scale_factor", lambda *a, **k: next(it))
    monkeypatch.setattr(sync, "SCALE_HYSTERESIS", hyst)


def test_first_call_adopts_raw(monkeypatch):
    _feed(monkeypatch, [0.00130])
    assert sync.get_stable_scale(1, 1) == 0.00130


def test_small_drift_holds_applied(monkeypatch):
    """差 4.6% < 10% → 沿用套用中值。"""
    _feed(monkeypatch, [0.00130, 0.00124])
    sync.get_stable_scale(1, 1)
    assert sync.get_stable_scale(1, 1) == 0.00130


def test_large_drift_adopts_new(monkeypatch):
    """差 10.8% >= 10% → 採用新值。"""
    _feed(monkeypatch, [0.00130, 0.00116])
    sync.get_stable_scale(1, 1)
    assert sync.get_stable_scale(1, 1) == 0.00116


def test_exact_band_boundary_adopts(monkeypatch):
    """恰好等於帶寬 → 採用（>=）。用二進位可精確表示的數避免浮點誤差。"""
    _feed(monkeypatch, [1.0, 0.875], hyst=0.125)   # |0.875-1.0| = 0.125 = 0.125×1.0
    sync.get_stable_scale(1, 1)
    assert sync.get_stable_scale(1, 1) == 0.875


def test_zero_band_disables_hysteresis(monkeypatch):
    """帶寬 0 = 停用，任何變動都直接採用（=現行為）。"""
    _feed(monkeypatch, [0.00130, 0.00129], hyst=0.0)
    sync.get_stable_scale(1, 1)
    assert sync.get_stable_scale(1, 1) == 0.00129


def test_sync_positions_uses_passed_scale(dry_trader, monkeypatch):
    """scale 參數傳入 → 不重算（單一計算點）。"""
    mock_csf = MagicMock(return_value=0.5)
    monkeypatch.setattr(sync, "compute_scale_factor", mock_csf)
    target_state = {"account_value": 1000.0, "positions": {}, "failed_dexs": set()}
    my_state = {"account_value": 100.0, "positions": {}}
    r = sync.sync_positions("http://x", dry_trader, target_state, my_state, scale=0.0007)
    mock_csf.assert_not_called()
    assert r["scale"] == 0.0007


def test_sync_positions_computes_scale_when_not_passed(dry_trader, monkeypatch):
    """未傳 scale → 維持現行內部計算。"""
    mock_csf = MagicMock(return_value=0.0009)
    monkeypatch.setattr(sync, "compute_scale_factor", mock_csf)
    target_state = {"account_value": 1000.0, "positions": {}, "failed_dexs": set()}
    my_state = {"account_value": 100.0, "positions": {}}
    r = sync.sync_positions("http://x", dry_trader, target_state, my_state)
    mock_csf.assert_called_once()
    assert r["scale"] == 0.0009
