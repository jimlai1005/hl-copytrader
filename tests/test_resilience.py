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
