"""結構守門：交易執行一定走 resilience 邊界，且重試邏輯只存在一處。"""
import pathlib

from src.trader import Trader

SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def test_trader_wraps_exchange_in_resilient_boundary():
    t = Trader(object(), None, live_trading=False)
    assert type(t.exchange).__name__ == "ResilientExchange"


def test_dry_trader_keeps_none_exchange():
    assert Trader(None, None).exchange is None


def test_no_stray_retry_helper_outside_resilience():
    offenders = [
        p.name for p in SRC.glob("*.py")
        if p.name != "resilience.py" and "_retry_transient" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"重試邏輯應只在 resilience.py，發現殘留: {offenders}"
