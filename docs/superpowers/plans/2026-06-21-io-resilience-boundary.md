# IO Resilience Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route trade execution through one resilience boundary so transient network failures recover within the cycle without ever duplicating a position/order, while normal behavior stays byte-identical.

**Architecture:** A new `src/resilience.py` holds the retry engine `run()` and a `ResilientExchange` wrapper around the SDK Exchange. `Trader.__init__` wraps its exchange in `ResilientExchange`, so every `self.exchange.*` call is resilient by construction. Idempotent calls (close/leverage/cancel/reduce-only order) retry directly; non-idempotent `market_open`/`place_order` use verify-then-retry (read-before-resend, biased to "assume landed"); `modify_order` keeps its existing cancel→replace fallback. Reads (`monitor`), notifications (`telegram`), and metadata (`instrument`) are out of scope.

**Tech Stack:** Python 3.9, pytest, monkeypatch (offline tests). Spec: `docs/superpowers/specs/2026-06-21-io-resilience-boundary-design.md`.

---

## File Structure

- **Create** `src/resilience.py` — the boundary: `run()` engine + `_is_transient_error` + constants + `VERIFIED_OK` + `ResilientExchange`.
- **Modify** `src/trader.py` — `__init__` wraps exchange; delete the old in-file retry helper/constants; convert close/leverage/xyz call sites to plain wrapper calls; add `_position_exists`/`_order_rests` verify helpers; `open_position`/`place_order` gain `my_address`/`api_url` and pass `_verify`.
- **Modify** `src/sync.py` — pass `my_address`/`api_url` into the `open_position` call.
- **Modify** `src/orders.py` — pass `my_address`/`api_url` into the two `place_order` calls.
- **Create** `tests/test_resilience.py` — engine + wrapper unit tests.
- **Create** `tests/test_resilience_boundary.py` — structural guard tests.
- **Modify** `tests/test_retry.py` — import from `resilience`, patch `resilience.time.sleep` (retry now lives in the engine; behavior unchanged).
- **Untouched:** `src/monitor.py`, `src/telegram.py`, `src/instrument.py`.

---

## Task 1: Resilience engine (`run` + classifier + constants)

**Files:**
- Create: `src/resilience.py`
- Test: `tests/test_resilience.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_resilience.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_resilience.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'src.resilience'`.

- [ ] **Step 3: Create `src/resilience.py` (engine only)**

```python
"""
單一 IO resilience 邊界（範圍：交易執行 = SDK Exchange 寫入）。
engine run() 做「分類 + 重試/驗證後重試」；ResilientExchange（Task 2）包住 SDK。
讀取(monitor)/通知(telegram)/meta(instrument) 不走這裡。
設計見 docs/superpowers/specs/2026-06-21-io-resilience-boundary-design.md。
"""
import logging
import time

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_BASE_DELAY = 0.6

# 驗證確認「其實已送達」時回傳的成功哨兵（原始 SDK 回應已隨斷線遺失）
VERIFIED_OK = {"status": "ok", "_resilience": "verified"}

_TRANSIENT_MARKERS = (
    "connection reset", "connection aborted", "connection broken",
    "remote end closed", "timed out", "timeout", "max retries",
    "temporarily unavailable", "bad gateway", "service unavailable",
    "502", "503", "504",
)


def _is_transient_error(exc: Exception) -> bool:
    """是否為可重試的暫時性網路錯誤，而非語意錯誤（保證金不足/訂單被拒）。"""
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True  # 內建 ConnectionResetError 屬 ConnectionError
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_MARKERS)


def run(fn, *, what, idempotent, verify=None, attempts=None,
        base_delay=RETRY_BASE_DELAY):
    """透過 resilience 邊界執行外部寫入 fn。
    - idempotent=True：暫時性錯誤直接重試（reduce-only/冪等，重送安全）。
    - idempotent=False 且 verify 提供：驗證後重試 —— 暫時性錯誤時呼叫 verify()，
      確認『已送達』→ 回 VERIFIED_OK 不重送；確認『沒送達』→ 才重送。
      verify 偏向『假設已送達』：查不出來一律當已送達，寧可漏跟也不重複下單。
    - idempotent=False 且 verify=None：只跑一次、暫時性錯誤直接拋出（=維持舊行為）。
    語意錯誤一律不重試、直接拋出（由呼叫端 except 告警）。
    """
    can_retry = idempotent or (verify is not None)
    if attempts is None:
        attempts = RETRY_ATTEMPTS if can_retry else 1
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if not _is_transient_error(e) or i == attempts or not can_retry:
                raise
            if not idempotent:  # 非冪等但有 verify → 驗證後決定是否重送
                try:
                    landed = verify()
                except Exception:
                    landed = True  # 查不出來 → 假設已送達
                if landed:
                    logger.warning(f"{what}：連線中斷但已驗證送達，視為成功（不重送）")
                    return VERIFIED_OK
            delay = base_delay * (2 ** (i - 1))
            logger.warning(
                f"{what}：暫時性錯誤（第 {i}/{attempts} 次），{delay:.1f}s 後重試: {e}"
            )
            time.sleep(delay)
    raise RuntimeError("resilience.run 未預期離開迴圈")  # pragma: no cover
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_resilience.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add src/resilience.py tests/test_resilience.py
git commit -m "feat(resilience): add IO boundary engine (classify + verify-then-retry)"
```

---

## Task 2: `ResilientExchange` wrapper

**Files:**
- Modify: `src/resilience.py` (append the class)
- Test: `tests/test_resilience.py` (append wrapper tests)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_resilience.py`:

```python
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_resilience.py -k wrapper -q`
Expected: FAIL with `AttributeError: module 'src.resilience' has no attribute 'ResilientExchange'`.

- [ ] **Step 3: Append `ResilientExchange` to `src/resilience.py`**

```python
class ResilientExchange:
    """包住 SDK Exchange，所有交易寫入經此分類。Trader 只持有這個包裝物件。
    冪等/ reduce-only → 直接重試；market_open / 非 reduce-only 掛單 → 驗證後重試
    （由呼叫端以 _verify 提供驗證）；modify_order → 不重試（已自帶 cancel→重掛 退回）。"""

    def __init__(self, exchange):
        self._ex = exchange

    # 冪等 / reduce-only → 直接重試
    def market_close(self, *a, **k):
        return run(lambda: self._ex.market_close(*a, **k),
                   what="平倉", idempotent=True)

    def update_leverage(self, *a, **k):
        return run(lambda: self._ex.update_leverage(*a, **k),
                   what="設定槓桿", idempotent=True)

    def cancel(self, *a, **k):
        return run(lambda: self._ex.cancel(*a, **k),
                   what="取消掛單", idempotent=True)

    # 非冪等 → 驗證後重試（無 _verify 則只跑一次、不重試）
    def market_open(self, *a, _verify=None, **k):
        return run(lambda: self._ex.market_open(*a, **k),
                   what="開倉", idempotent=False, verify=_verify)

    def order(self, *a, reduce_only=False, _verify=None, **k):
        idem = bool(reduce_only)  # reduce-only 掛單冪等、可直接重試
        return run(lambda: self._ex.order(*a, reduce_only=reduce_only, **k),
                   what="掛單", idempotent=idem,
                   verify=(None if idem else _verify))

    # 已自帶 cancel→重掛 退回機制 → 不加重試
    def modify_order(self, *a, **k):
        return run(lambda: self._ex.modify_order(*a, **k),
                   what="改單", idempotent=False)
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3 -m pytest tests/test_resilience.py -q`
Expected: PASS (13 passed).

- [ ] **Step 5: Commit**

```bash
git add src/resilience.py tests/test_resilience.py
git commit -m "feat(resilience): add ResilientExchange wrapper with per-method classification"
```

---

## Task 3: Wire `Trader` to the boundary; remove the old in-file retry helper

This task makes `Trader` route through the wrapper and deletes the now-duplicate retry code from `trader.py`. Existing `test_retry.py` keeps passing because the wrapper provides the same retry — but its imports/patch target move to `resilience`.

**Files:**
- Modify: `src/trader.py`
- Modify: `tests/test_retry.py`

- [ ] **Step 1: Update `tests/test_retry.py` imports and sleep patch (still asserts the same behavior)**

Replace the top of `tests/test_retry.py` (imports + `_no_sleep` fixture) with:

```python
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
```

Leave the rest of `tests/test_retry.py` (FlakyExchange, FakeInfo, the close/leverage/semantic tests) unchanged.

- [ ] **Step 2: Run to confirm the suite is currently green (baseline)**

Run: `python3 -m pytest tests/test_retry.py -q`
Expected: PASS (still 5 passed — behavior unchanged; the test now patches the engine's sleep).

> Note: this passes even before the trader change because `Trader` still has its own `_retry_transient`. The next steps move retry into the wrapper; these tests must stay green throughout.

- [ ] **Step 3: Edit `src/trader.py` — imports and delete the old retry block**

In `src/trader.py`, add the resilience import near the other `from .` imports (after `from .config import ORDER_LEVERAGE`):

```python
from .resilience import ResilientExchange
```

Then **delete** the entire in-file retry block — the constants and both functions:
- `RETRY_ATTEMPTS = 3`
- `RETRY_BASE_DELAY = 0.6`
- `_TRANSIENT_MARKERS = (...)`
- `def _is_transient_error(...)`
- `def _retry_transient(...)`

(Keep `ENTRY_LEVERAGE_FALLBACK = 20`. Keep `import time` — it is still used by `close_position`'s `close_ts`.)

- [ ] **Step 4: Edit `src/trader.py` — `__init__` wraps the exchange**

Replace the `__init__` body's first line:

```python
    def __init__(self, exchange, info, live_trading: bool = False):
        self.exchange = ResilientExchange(exchange) if exchange is not None else None
        self.info = info
        self.live_trading = live_trading
        self._sz_dec: dict = {}
        self._max_lev: dict = {}
        self._only_iso: dict = {}
```

(`dry_trader` passes `exchange=None`; it never calls the exchange, so leaving it `None` is correct.)

- [ ] **Step 5: Edit `src/trader.py` — convert the three idempotent call sites to plain wrapper calls**

In `set_leverage`, replace the `_retry_transient(...)` call with:

```python
            result = self.exchange.update_leverage(leverage, coin, is_cross)
```

In `close_position`, replace the `else` branch `_retry_transient(...)` with:

```python
            else:
                result = self.exchange.market_close(coin, size)
```

In `_close_xyz`, replace the `return _retry_transient(...)` with:

```python
        # reduce-only IoC → 包裝層判為冪等、直接重試（不會反向超平）
        return self.exchange.order(
            coin, close_is_buy, size, adj_px,
            order_type={"limit": {"tif": "Ioc"}},
            reduce_only=True,
        )
```

- [ ] **Step 6: Run the affected tests**

Run: `python3 -m pytest tests/test_retry.py tests/test_live_execution.py -q`
Expected: PASS (close/leverage still retry via the wrapper; characterization tests unchanged because the wrapper forwards identical SDK calls/args).

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (45 passed: prior 40 + 13 new resilience − 0; count is informational, the requirement is all green).

- [ ] **Step 8: Commit**

```bash
git add src/trader.py tests/test_retry.py
git commit -m "refactor(trader): route exchange through ResilientExchange; drop in-file retry"
```

---

## Task 4: `open_position` verify-then-retry

Give `market_open` within-cycle recovery without duplicating a position: on a transient failure, re-check positions; only resend if no position exists (biased to assume-landed).

**Files:**
- Modify: `src/trader.py` (`open_position` + new `_position_exists` helper + `adjust_position` internal calls)
- Modify: `src/sync.py` (thread `my_address`/`api_url` into `open_position`)
- Test: `tests/test_resilience_boundary.py` (created here; guard tests added in Task 6)

- [ ] **Step 1: Write the failing test**

Create `tests/test_open_verify.py`:

```python
import pytest

from src import monitor
from src.trader import Trader


CONN = ConnectionError(
    "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"
)


class OpenFlakySDK:
    """market_open 一律丟暫時性錯誤；其餘呼叫成功。"""
    def __init__(self):
        self.open_calls = 0

    def update_leverage(self, *a, **k):
        return {"status": "ok"}

    def market_open(self, *a, **k):
        self.open_calls += 1
        raise CONN


class FakeInfo:
    def meta(self, dex=""):
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    from src import resilience
    monkeypatch.setattr(resilience.time, "sleep", lambda *_a, **_k: None)


def test_open_skips_resend_when_position_exists(monkeypatch):
    monkeypatch.setattr(monitor, "get_my_state",
                        lambda api, addr: {"positions": {"BTC": {"size": 0.01}},
                                           "account_value": 0.0})
    sdk = OpenFlakySDK()
    t = Trader(sdk, FakeInfo(), live_trading=True)
    t.open_position("BTC", True, 0.01, 20, True, entry_px=60000.0,
                    my_address="0xme", api_url="http://x")
    assert sdk.open_calls == 1   # 連線斷但部位已存在 → 不重送


def test_open_resends_when_position_absent(monkeypatch):
    state = {"positions": {}, "account_value": 0.0}
    monkeypatch.setattr(monitor, "get_my_state", lambda api, addr: state)
    sdk = OpenFlakySDK()
    t = Trader(sdk, FakeInfo(), live_trading=True)
    t.open_position("BTC", True, 0.01, 20, True, entry_px=60000.0,
                    my_address="0xme", api_url="http://x")
    assert sdk.open_calls == 3   # 確認沒部位 → 重送（用盡重試）
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_open_verify.py -q`
Expected: FAIL — `open_position()` got an unexpected keyword argument `my_address` (signature not updated yet).

- [ ] **Step 3: Add `_position_exists` helper to `src/trader.py`**

Add near the top of `src/trader.py`, after `logger = logging.getLogger(__name__)`:

```python
def _position_exists(api_url: str, my_address: str, coin: str) -> bool:
    """驗證開倉是否已送達：偏向『假設已送達』。
    只有讀到部位、且確認該 coin『沒有部位』時才回 False（→ 允許重送）。
    無法查詢（缺位址 / 讀取失敗）一律回 True（假設已送達，絕不重複開倉）。"""
    if not (api_url and my_address):
        return True
    try:
        from .monitor import get_my_state
        return coin in get_my_state(api_url, my_address)["positions"]
    except Exception:
        return True
```

- [ ] **Step 4: Update `open_position` signature and `market_open` calls in `src/trader.py`**

Change the signature to accept `my_address`/`api_url`:

```python
    def open_position(self, coin: str, is_buy: bool, size: float,
                      leverage: int, is_cross: bool,
                      entry_px: float = 0, scale: float = 0,
                      trader_account: float = 0,
                      my_address: str = "", api_url: str = "") -> Optional[dict]:
```

Replace the `try` block's `market_open` branch with verify-passing calls:

```python
        self.set_leverage(coin, leverage, is_cross)
        verify = lambda: _position_exists(api_url, my_address, coin)
        try:
            if ":" in coin:
                # xyz DEX：傳入已知 mid price 繞過 SDK 取價缺陷
                result = self.exchange.market_open(coin, is_buy, size, px=entry_px,
                                                   _verify=verify)
            else:
                result = self.exchange.market_open(coin, is_buy, size, _verify=verify)
```

(The rest of `open_position` — the `_extract_order_error(result)` check, `tg.notify_open`, and `except` — is unchanged. `VERIFIED_OK` has no error statuses, so `_extract_order_error` returns falsy and it is treated as success.)

- [ ] **Step 5: Update `adjust_position`'s two internal `open_position` calls in `src/trader.py`**

`adjust_position` already receives `my_address`/`api_url`. Pass them through both internal opens. The direction-change reopen:

```python
            self.open_position(coin, is_buy_to_open, target_size, leverage, is_cross,
                               entry_px=entry_px, scale=scale,
                               trader_account=trader_account,
                               my_address=my_address, api_url=api_url)
```

And the add-up open:

```python
            self.open_position(coin, is_buy, diff, leverage, is_cross,
                               entry_px=entry_px, scale=scale,
                               trader_account=trader_account,
                               my_address=my_address, api_url=api_url)
```

- [ ] **Step 6: Thread context from `src/sync.py`**

In `sync_positions`, the new-open call (currently `trader.open_position(...)`) becomes:

```python
            result = trader.open_position(
                coin, is_buy, target_size, leverage, is_cross,
                entry_px=mid_px, scale=scale, trader_account=trader_account_value,
                my_address=my_address, api_url=api_url,
            )
```

- [ ] **Step 7: Run the test**

Run: `python3 -m pytest tests/test_open_verify.py -q`
Expected: PASS (2 passed).

- [ ] **Step 8: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all green; existing open tests unaffected — default `my_address=""` makes `verify` return True, and with no failure the SDK call runs once).

- [ ] **Step 9: Commit**

```bash
git add src/trader.py src/sync.py tests/test_open_verify.py
git commit -m "feat(trader): verify-then-retry for market_open (no duplicate positions)"
```

---

## Task 5: `place_order` verify-then-retry

Same pattern for limit placement: on a transient failure, re-check open orders; only resend if no matching order rests.

**Files:**
- Modify: `src/trader.py` (`place_order` + new `_order_rests` helper)
- Modify: `src/orders.py` (thread `my_address`/`api_url` into both `place_order` calls)
- Test: `tests/test_place_verify.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_place_verify.py`:

```python
import pytest

from src import monitor
from src.trader import Trader


CONN = ConnectionError(
    "('Connection aborted.', ConnectionResetError(54, 'Connection reset by peer'))"
)


class OrderFlakySDK:
    """order 一律丟暫時性錯誤；其餘成功。"""
    def __init__(self):
        self.order_calls = 0

    def order(self, *a, **k):
        self.order_calls += 1
        raise CONN


class FakeInfo:
    def meta(self, dex=""):
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


def _spec(coin="BTC", is_buy=True, size=0.01, limit_px=60000.0):
    return {"coin": coin, "is_buy": is_buy, "size": size, "limit_px": limit_px,
            "trigger_px": 0.0, "reduce_only": False, "is_trigger": False,
            "tpsl": None, "is_market": False, "tif": "Gtc",
            "order_type_name": "Limit"}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    from src import resilience
    monkeypatch.setattr(resilience.time, "sleep", lambda *_a, **_k: None)


def test_place_skips_resend_when_order_rests(monkeypatch):
    resting = [{"coin": "BTC", "is_buy": True, "limit_px": 60000.0, "size": 0.01}]
    monkeypatch.setattr(monitor, "get_my_open_orders", lambda api, addr: resting)
    sdk = OrderFlakySDK()
    t = Trader(sdk, FakeInfo(), live_trading=True)
    ok, _ = t.place_order(_spec(), my_address="0xme", api_url="http://x")
    assert sdk.order_calls == 1   # 連線斷但掛單已在 → 不重送
    assert ok is True


def test_place_resends_when_order_absent(monkeypatch):
    monkeypatch.setattr(monitor, "get_my_open_orders", lambda api, addr: [])
    sdk = OrderFlakySDK()
    t = Trader(sdk, FakeInfo(), live_trading=True)
    t.place_order(_spec(), my_address="0xme", api_url="http://x")
    assert sdk.order_calls == 3   # 確認沒這張 → 重送（用盡重試）
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_place_verify.py -q`
Expected: FAIL — `place_order()` got an unexpected keyword argument `my_address`.

- [ ] **Step 3: Add `_order_rests` helper to `src/trader.py`**

Add after `_position_exists`:

```python
def _order_rests(api_url: str, my_address: str, spec: dict, px: float) -> bool:
    """驗證掛單是否已送達：偏向『假設已送達』。
    只有讀到掛單清單、且確認沒有相符掛單（同 coin/方向、價格相近）時才回 False。
    無法查詢一律回 True（假設已送達，絕不重複掛單）。
    對帳不變式：place_order 只在『目前沒有相符掛單』時才被呼叫，故事後出現相符掛單
    即為我們這張。"""
    if not (api_url and my_address):
        return True
    try:
        from .monitor import get_my_open_orders
        orders = get_my_open_orders(api_url, my_address)
    except Exception:
        return True
    tol = max(px * 0.001, 1e-9)
    for o in orders:
        if o["coin"] == spec["coin"] and o["is_buy"] == spec["is_buy"]:
            if px <= 0 or abs(o["limit_px"] - px) <= tol:
                return True
    return False
```

- [ ] **Step 4: Update `place_order` signature and the `order` call in `src/trader.py`**

Change the signature:

```python
    def place_order(self, spec: dict, my_address: str = "",
                    api_url: str = "") -> tuple:
```

Replace the `try` block's `self.exchange.order(...)` call with a verify-passing call:

```python
        try:
            verify = lambda: _order_rests(api_url, my_address, spec, px)
            result = self.exchange.order(
                coin, is_buy, size, px,
                order_type=order_type, reduce_only=reduce_only,
                _verify=verify,
            )
```

(The rest of `place_order` — `_extract_order_error(result)`, `_route_order_error`, `tg.notify_order_placed`, return values — is unchanged. For reduce-only specs the wrapper ignores `_verify` and retries directly; `VERIFIED_OK` yields no error and is treated as a successful placement.)

- [ ] **Step 5: Thread context from `src/orders.py` (both call sites in `_reconcile_orders`)**

The first placement loop:

```python
        ok, _ = trader.place_order(d, my_address=my_address, api_url=api_url)
```

The verification-retry placement loop:

```python
                ok, _ = trader.place_order(d, my_address=my_address, api_url=api_url)
```

- [ ] **Step 6: Run the test**

Run: `python3 -m pytest tests/test_place_verify.py -q`
Expected: PASS (2 passed).

- [ ] **Step 7: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all green; `test_live_execution.py` place tests unaffected — `_verify` is stripped by the wrapper before the SDK call, so recorded args are unchanged, and with no failure the verify is never invoked).

- [ ] **Step 8: Commit**

```bash
git add src/trader.py src/orders.py tests/test_place_verify.py
git commit -m "feat(trader): verify-then-retry for place_order (no duplicate orders)"
```

---

## Task 6: Structural guard test

Lock the boundary in place so future code cannot bypass it.

**Files:**
- Create: `tests/test_resilience_boundary.py`

- [ ] **Step 1: Write the test**

Create `tests/test_resilience_boundary.py`:

```python
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
```

- [ ] **Step 2: Run the test**

Run: `python3 -m pytest tests/test_resilience_boundary.py -q`
Expected: PASS (3 passed).

- [ ] **Step 3: Run the full suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS — all green (≈52 tests: original 40 + new resilience/wrapper/open/place/guard).

- [ ] **Step 4: Commit**

```bash
git add tests/test_resilience_boundary.py
git commit -m "test(resilience): structural guard — trade execution cannot bypass the boundary"
```

---

## Self-Review notes (for the implementer)

- **Behavior preservation:** every change only affects *failure* paths. With no exception thrown, each path runs exactly once, identical to today. Non-idempotent `open`/`place` never blind-resend; `modify` still falls back to cancel→replace.
- **Existing tests are the safety net:** `test_live_execution.py` (characterization) and `test_retry.py` (close/leverage retry) must stay green at every task boundary. If either breaks, the refactor changed observable behavior — stop and investigate.
- **No network in tests:** monitor reads are monkeypatched; `telegram._send` is muted by the autouse conftest fixture; `resilience.time.sleep` is patched in retry tests.
- **Out of scope (do NOT touch):** `src/monitor.py`, `src/telegram.py`, `src/instrument.py`. There is intentionally no "all `requests.post` must go through the boundary" rule.
