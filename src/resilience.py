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
