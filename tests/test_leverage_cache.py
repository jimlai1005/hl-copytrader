"""set_leverage 快取：同 coin 同 (leverage, is_cross) 成功設過就跳過，
省下大量重複的 update_leverage 請求（避免 Hyperliquid 限流）。
槓桿在交易所是 per-coin 持久狀態，重設同值不改變任何交易行為。"""
from src.trader import Trader


class LevExchange:
    """記錄 update_leverage 被真正呼叫幾次。"""
    def __init__(self, result=None):
        self.calls = 0
        self.result = result if result is not None else {"status": "ok"}

    def update_leverage(self, leverage, coin, is_cross):
        self.calls += 1
        return self.result


def _t(ex):
    return Trader(ex, None, live_trading=True)


def test_cached_after_success():
    ex = LevExchange()
    t = _t(ex)
    assert t.set_leverage("BTC", 20, True) is True
    assert t.set_leverage("BTC", 20, True) is True   # 快取命中
    assert t.set_leverage("BTC", 20, True) is True
    assert ex.calls == 1                              # 只送一次請求


def test_resent_when_leverage_or_cross_changes():
    ex = LevExchange()
    t = _t(ex)
    t.set_leverage("BTC", 20, True)
    t.set_leverage("BTC", 10, True)     # 槓桿變了 → 重設
    t.set_leverage("BTC", 10, False)    # cross→isolated → 重設
    assert ex.calls == 3


def test_error_not_cached():
    ex = LevExchange(result={"status": "err", "response": "Cross margin is not allowed"})
    t = _t(ex)
    assert t.set_leverage("BTC", 20, True) is False
    assert t.set_leverage("BTC", 20, True) is False   # 失敗不快取 → 會再試
    assert ex.calls == 2


def test_cache_is_per_coin():
    ex = LevExchange()
    t = _t(ex)
    t.set_leverage("BTC", 20, True)
    t.set_leverage("ETH", 20, True)     # 不同 coin → 各設一次
    t.set_leverage("BTC", 20, True)     # BTC 已快取 → 跳過
    assert ex.calls == 2


def test_dry_run_does_not_touch_exchange():
    ex = LevExchange()
    t = Trader(ex, None, live_trading=False)
    assert t.set_leverage("BTC", 20, True) is True
    assert ex.calls == 0                              # dry-run 不打 API（行為不變）
