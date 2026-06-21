"""NOTIFY_CLOSES 開關：預設開（發平倉通知），設 false 則不發。"""
from src import telegram


def test_notify_close_sends_when_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(telegram, "NOTIFY_CLOSES", True)
    telegram.notify_close("BTC", "long", 0.1, 5.0)
    assert len(calls) == 1


def test_notify_close_silent_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(telegram, "NOTIFY_CLOSES", False)
    telegram.notify_close("BTC", "long", 0.1, 5.0)
    assert calls == []   # 關閉後不發
