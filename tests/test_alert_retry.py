# tests/test_alert_retry.py
"""關鍵一次性告警（回撤停機、機器人停止、被清算）在 Telegram 網路失敗時重試。
為何：7 天 log 有 23 次 Telegram 網路失敗；_send 原本失敗只記 warning 不重試，
熔斷那則訊息若剛好碰到就丟了，使用者不會知道程式已停。"""
import src.telegram as telegram

REAL_SEND = telegram._send     # 在 conftest 的 autouse mute 之前取得真函式（模組載入時）


def _arm(monkeypatch):
    monkeypatch.setattr(telegram, "_BOT_TOKEN", "t")
    monkeypatch.setattr(telegram, "_CHAT_ID", "c")
    monkeypatch.setattr(telegram, "_recent_sent", {})
    monkeypatch.setattr(telegram._time, "sleep", lambda s: None)


class _Resp:
    def __init__(self, ok):
        self.ok = ok; self.status_code = 200 if ok else 500; self.text = "x"


def test_send_retries_until_success(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}
    def post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset")
        return _Resp(True)
    monkeypatch.setattr(telegram.requests, "post", post)
    assert REAL_SEND("hi", retries=2) is True
    assert calls["n"] == 3


def test_send_gives_up_after_retries(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or _Resp(False))
    assert REAL_SEND("hi", retries=2) is False
    assert calls["n"] == 3


def test_send_default_no_retry(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}
    def post(*a, **k):
        calls["n"] += 1
        raise ConnectionError("reset")
    monkeypatch.setattr(telegram.requests, "post", post)
    assert REAL_SEND("hi") is False
    assert calls["n"] == 1


def test_dedup_checked_once_not_per_attempt(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}
    def post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("reset")
        return _Resp(True)
    monkeypatch.setattr(telegram.requests, "post", post)
    assert REAL_SEND("hi", dedup_key="k", retries=2) is True     # 第 2 次嘗試成功
    assert REAL_SEND("hi", dedup_key="k", retries=2) is False    # 5 分鐘內同 key → 去重


def test_critical_alerts_request_retries(monkeypatch):
    seen = []
    monkeypatch.setattr(telegram, "_send", lambda text, **kw: seen.append(kw.get("retries", 0)) or True)
    telegram.alert_drawdown(800.0, 1000.0, 0.2)
    telegram.alert_bot_stopped("回撤超過上限")
    telegram.alert_liquidated("xyz:CL", -9.5, "09/13 08:00")
    assert seen == [2, 2, 2]
