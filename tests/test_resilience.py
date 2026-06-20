import pytest
from src import resilience
from src.resilience import run, _is_transient_error, VERIFIED_OK, RETRY_ATTEMPTS

CONN = ConnectionError(
    "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"
)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(resilience.time, "sleep", lambda *_a, **_k: None)


class Counter:
    """前 fail_times 次呼叫丟 exc，之後回 ret；記錄被呼叫次數。"""
    def __init__(self, fail_times, exc, ret="OK"):
        self.n = 0
        self.fail_times = fail_times
        self.exc = exc
        self.ret = ret

    def __call__(self):
        self.n += 1
        if self.n <= self.fail_times:
            raise self.exc
        return self.ret


def test_classifier():
    assert _is_transient_error(CONN)
    assert _is_transient_error(TimeoutError("timed out"))
    assert _is_transient_error(Exception("502 Bad Gateway"))
    assert not _is_transient_error(ValueError("Insufficient margin"))


def test_idempotent_retries_then_succeeds():
    fn = Counter(fail_times=2, exc=CONN)
    assert run(fn, what="x", idempotent=True) == "OK"
    assert fn.n == 3


def test_idempotent_gives_up_after_attempts():
    fn = Counter(fail_times=99, exc=CONN)
    with pytest.raises(ConnectionError):
        run(fn, what="x", idempotent=True)
    assert fn.n == RETRY_ATTEMPTS


def test_semantic_not_retried():
    fn = Counter(fail_times=99, exc=ValueError("rejected"))
    with pytest.raises(ValueError):
        run(fn, what="x", idempotent=True)
    assert fn.n == 1


def test_non_idempotent_no_verify_runs_once():
    fn = Counter(fail_times=99, exc=CONN)
    with pytest.raises(ConnectionError):
        run(fn, what="x", idempotent=False)
    assert fn.n == 1


def test_verify_landed_returns_sentinel_without_resend():
    fn = Counter(fail_times=99, exc=CONN)
    result = run(fn, what="x", idempotent=False, verify=lambda: True)
    assert result == VERIFIED_OK
    assert fn.n == 1


def test_verify_not_landed_resends_then_succeeds():
    fn = Counter(fail_times=1, exc=CONN)
    result = run(fn, what="x", idempotent=False, verify=lambda: False)
    assert result == "OK"
    assert fn.n == 2


def test_verify_read_failure_assumes_landed():
    fn = Counter(fail_times=99, exc=CONN)

    def boom():
        raise RuntimeError("read failed")

    result = run(fn, what="x", idempotent=False, verify=boom)
    assert result == VERIFIED_OK
    assert fn.n == 1


class FakeSDK:
    """記錄每次呼叫；可設定哪個方法前幾次丟暫時性錯誤。"""
    def __init__(self, fail=None, exc=CONN):
        self.calls = []
        self.fail = fail or {}      # {"market_open": 99, ...}
        self.exc = exc
        self.counts = {}

    def _do(self, name, a, k, ret):
        self.counts[name] = self.counts.get(name, 0) + 1
        self.calls.append((name, a, k))
        if self.counts[name] <= self.fail.get(name, 0):
            raise self.exc
        return ret

    def market_close(self, *a, **k): return self._do("market_close", a, k, "closed")
    def market_open(self, *a, **k):  return self._do("market_open", a, k, "opened")
    def order(self, *a, **k):        return self._do("order", a, k, "ordered")
    def update_leverage(self, *a, **k): return self._do("update_leverage", a, k, "lev")
    def cancel(self, *a, **k):       return self._do("cancel", a, k, "cancelled")
    def modify_order(self, *a, **k): return self._do("modify_order", a, k, "modified")


def test_wrapper_strips_verify_before_sdk():
    sdk = FakeSDK()
    rex = resilience.ResilientExchange(sdk)
    rex.order("BTC", True, 0.1, 60000.0,
              order_type={"limit": {"tif": "Gtc"}}, reduce_only=False,
              _verify=lambda: True)
    name, a, k = sdk.calls[-1]
    assert name == "order"
    assert "_verify" not in k          # 包裝層吃掉 _verify，不傳給 SDK
    assert k["reduce_only"] is False
    assert a == ("BTC", True, 0.1, 60000.0)


def test_wrapper_close_retries_idempotent():
    sdk = FakeSDK(fail={"market_close": 2})
    rex = resilience.ResilientExchange(sdk)
    assert rex.market_close("BTC", 0.1) == "closed"
    assert sdk.counts["market_close"] == 3


def test_wrapper_reduce_only_order_retries_directly():
    sdk = FakeSDK(fail={"order": 2})
    rex = resilience.ResilientExchange(sdk)
    rex.order("xyz:NVDA", False, 1.0, 100.0,
              order_type={"limit": {"tif": "Ioc"}}, reduce_only=True)
    assert sdk.counts["order"] == 3    # reduce_only → 冪等 → 直接重試


def test_wrapper_market_open_verify_then_skip_resend():
    sdk = FakeSDK(fail={"market_open": 99})
    rex = resilience.ResilientExchange(sdk)
    result = rex.market_open("BTC", True, 0.1, _verify=lambda: True)
    assert result == VERIFIED_OK
    assert sdk.counts["market_open"] == 1   # 驗證已送達 → 不重送


def test_wrapper_modify_does_not_retry():
    sdk = FakeSDK(fail={"modify_order": 99})
    rex = resilience.ResilientExchange(sdk)
    with pytest.raises(ConnectionError):
        rex.modify_order(123, "BTC", True, 0.1, 60000.0, {"limit": {"tif": "Gtc"}})
    assert sdk.counts["modify_order"] == 1
