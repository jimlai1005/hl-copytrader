"""波動通知的每小時節流：只在『發送成功』後才前進時間戳，
避免一次暫時性失敗就把通知鎖死一小時（線上事故根因）。"""
from src import telegram


def _stats():
    return {"z": -2.5, "today": 2, "mu": 13, "sigma": 4, "weight": 1.0, "days": 5}


def test_failed_send_does_not_advance_throttle(monkeypatch):
    monkeypatch.setattr(telegram, "NOTIFY_VOLATILITY", True)
    telegram._vol_last_sent["ts"] = 0.0
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: False)   # 模擬發送失敗
    telegram.notify_account_volatility(_stats())
    assert telegram._vol_last_sent["ts"] == 0.0   # 失敗 → 不前進 → 下一輪會重試


def test_successful_send_advances_then_throttles(monkeypatch):
    monkeypatch.setattr(telegram, "NOTIFY_VOLATILITY", True)
    telegram._vol_last_sent["ts"] = 0.0
    calls = []
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: (calls.append(1), True)[1])
    telegram.notify_account_volatility(_stats())
    assert telegram._vol_last_sent["ts"] > 0.0    # 成功 → 前進節流
    assert len(calls) == 1
    telegram.notify_account_volatility(_stats())   # 一小時內再呼叫
    assert len(calls) == 1                          # 被節流，不重發
    telegram._vol_last_sent["ts"] = 0.0             # 還原，避免影響其他測試
