import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.trader import Trader


@pytest.fixture(autouse=True)
def _offline_weight(monkeypatch):
    """測試預設停用波動權重，避免 compute_scale_factor 走 portfolio 網路 API。"""
    from src import weight
    monkeypatch.setattr(weight, "get_vol_stats", lambda address: None)


@pytest.fixture
def dry_trader():
    """乾跑 Trader：不連線、不下單；下單/平倉只回 dry_run。"""
    t = Trader(None, None, live_trading=False)
    # 預填 size decimals，避免在無 info 時走 meta 查詢
    t._sz_dec = {"BTC": 5, "ETH": 4, "xyz:NVDA": 2, "SOL": 2}
    return t


def make_pos(coin, side="long", size=1.0, notional=100.0, leverage=10,
             lev_type="cross", upnl=0.0, entry_px=100.0):
    return {
        "coin": coin, "side": side, "size": size, "notional": notional,
        "leverage": leverage, "leverage_type": lev_type,
        "unrealized_pnl": upnl, "entry_px": entry_px,
    }
