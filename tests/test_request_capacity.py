"""可用 API request 額度：monitor.get_request_capacity 解析、以及波動通知附上該行。
額度查詢只在真的要發(通過每小時節流)時才做 → 用 get_remaining callable 延遲取值。"""
from src import monitor, telegram


def test_get_request_capacity(monkeypatch):
    monkeypatch.setattr(monitor, "_post", lambda api, payload: {
        "nRequestsUsed": 44461, "nRequestsCap": 50326, "cumVlm": "40326.27"})
    assert monitor.get_request_capacity("api", "0x") == 50326 - 44461   # 5865


def test_get_request_capacity_over_quota_negative(monkeypatch):
    monkeypatch.setattr(monitor, "_post", lambda api, payload: {
        "nRequestsUsed": 44443, "nRequestsCap": 40336})
    assert monitor.get_request_capacity("api", "0x") == 40336 - 44443   # -4107（超額）


def test_get_request_capacity_failure(monkeypatch):
    def boom(api, payload):
        raise RuntimeError("net")
    monkeypatch.setattr(monitor, "_post", boom)
    assert monitor.get_request_capacity("api", "0x") is None


def _stats():
    return {"z": -0.5, "today": 8, "mu": 14, "sigma": 10, "weight": 1.0, "days": 14}


def test_volatility_message_appends_remaining(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "_send", lambda msg, **k: (sent.append(msg), True)[1])
    monkeypatch.setattr(telegram, "NOTIFY_VOLATILITY", True)
    telegram._vol_last_sent["ts"] = 0.0
    telegram.notify_account_volatility(_stats(), get_remaining=lambda: 5865)
    assert "可用Request" in sent[0] and "5,865" in sent[0]
    telegram._vol_last_sent["ts"] = 0.0


def test_volatility_message_without_get_remaining(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "_send", lambda msg, **k: (sent.append(msg), True)[1])
    monkeypatch.setattr(telegram, "NOTIFY_VOLATILITY", True)
    telegram._vol_last_sent["ts"] = 0.0
    telegram.notify_account_volatility(_stats())          # 沒給 get_remaining
    assert "可用Request" not in sent[0]
    telegram._vol_last_sent["ts"] = 0.0


def test_volatility_remaining_fetch_error_no_crash(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "_send", lambda msg, **k: (sent.append(msg), True)[1])
    monkeypatch.setattr(telegram, "NOTIFY_VOLATILITY", True)
    telegram._vol_last_sent["ts"] = 0.0

    def boom():
        raise RuntimeError("info down")

    telegram.notify_account_volatility(_stats(), get_remaining=boom)   # 不應 crash
    assert sent and "可用Request" not in sent[0]        # 查失敗 → 不加該行，訊息照常發
    telegram._vol_last_sent["ts"] = 0.0
