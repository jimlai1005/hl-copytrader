"""平倉/設定槓桿遇『暫時性網路錯誤』(連線重置/逾時)會重試，不會一次就放棄。

重現線上事故：目標已平倉，但我方 market_close 碰到
ConnectionResetError('Connection reset by peer') 就 return None、沒重試，
導致目標已平、我方仍持倉、曝險到下一輪（非活躍時段最久達一小時）。
"""
import pytest

from src import resilience
from src.trader import Trader
from src.resilience import _is_transient_error, RETRY_ATTEMPTS

# 線上 log 實際出現的例外字串（requests 包裝內建 ConnectionResetError）
CONN_RESET = ConnectionError(
    "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """略過重試退避的真實 sleep（重試現在在 resilience 引擎內）。"""
    monkeypatch.setattr(resilience.time, "sleep", lambda *_a, **_k: None)


class FlakyExchange:
    """前 fail_times 次呼叫丟 exc，之後回 OK；分別計數 close 與 leverage。"""
    OK = {"status": "ok", "response": {"data": {"statuses": [{}]}}}

    def __init__(self, fail_times, exc):
        self.fail_times = fail_times
        self.exc = exc
        self.close_calls = 0
        self.lev_calls = 0

    def market_close(self, coin, size):
        self.close_calls += 1
        if self.close_calls <= self.fail_times:
            raise self.exc
        return self.OK

    def update_leverage(self, leverage, coin, is_cross):
        self.lev_calls += 1
        if self.lev_calls <= self.fail_times:
            raise self.exc
        return self.OK


class FakeInfo:
    def meta(self, dex=""):
        return {"universe": [{"name": "HYPE", "szDecimals": 2, "maxLeverage": 20}]}


def _live(ex):
    return Trader(ex, FakeInfo(), live_trading=True)


# ── _is_transient_error 辨識 ────────────────────────────────────────────
def test_transient_classifier():
    assert _is_transient_error(CONN_RESET)
    assert _is_transient_error(TimeoutError("timed out"))
    assert _is_transient_error(Exception("502 Bad Gateway"))
    # 語意錯誤不該被當暫時性
    assert not _is_transient_error(ValueError("Insufficient margin"))
    assert not _is_transient_error(Exception("Order has invalid size"))


# ── 平倉重試 ────────────────────────────────────────────────────────────
def test_close_retries_transient_then_succeeds():
    ex = FlakyExchange(fail_times=2, exc=CONN_RESET)
    result = _live(ex).close_position("HYPE", is_buy=True, size=2.74)
    assert ex.close_calls == 3          # 2 次連線重置 + 第 3 次成功
    assert result == FlakyExchange.OK


def test_close_gives_up_after_max_attempts():
    ex = FlakyExchange(fail_times=99, exc=CONN_RESET)
    result = _live(ex).close_position("HYPE", is_buy=True, size=2.74)
    assert ex.close_calls == RETRY_ATTEMPTS   # 用盡重試
    assert result is None                     # 安全失敗；下一輪會再嘗試平倉


def test_close_does_not_retry_semantic_error():
    ex = FlakyExchange(fail_times=99, exc=ValueError("order rejected"))
    result = _live(ex).close_position("HYPE", is_buy=True, size=2.74)
    assert ex.close_calls == 1                 # 非暫時性 → 不重試，立即放棄
    assert result is None


# ── 設定槓桿重試（冪等）─────────────────────────────────────────────────
def test_set_leverage_retries_transient_then_succeeds():
    ex = FlakyExchange(fail_times=1, exc=CONN_RESET)
    assert _live(ex).set_leverage("HYPE", 20, is_cross=True) is True
    assert ex.lev_calls == 2
