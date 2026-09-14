# tests/test_wallet_tag.py
"""Telegram 訊息加錢包識別前綴（_send 單一注入點）。
為何：兩台機器各跑一顆錢包、共用同一個 Telegram bot，需要在訊息前加識別才能分辨來源。"""
import src.telegram as telegram

REAL_SEND = telegram._send     # 在 conftest 的 autouse mute 之前取得真函式（模組載入時）


def _arm(monkeypatch):
    monkeypatch.setattr(telegram, "_BOT_TOKEN", "t")
    monkeypatch.setattr(telegram, "_CHAT_ID", "c")
    monkeypatch.setattr(telegram, "_recent_sent", {})


class _Resp:
    def __init__(self, ok):
        self.ok = ok; self.status_code = 200 if ok else 500; self.text = "x"


def _capture_post(monkeypatch):
    captured = {}

    def post(url, json=None, timeout=None):
        captured["text"] = json["text"]
        return _Resp(True)

    monkeypatch.setattr(telegram.requests, "post", post)
    return captured


def test_send_adds_prefix_when_tag_set(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(telegram, "_WALLET_TAG", "新機10k")
    captured = _capture_post(monkeypatch)
    assert REAL_SEND("hi") is True
    assert captured["text"] == "[新機10k]\nhi"


def test_send_no_prefix_when_tag_empty(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(telegram, "_WALLET_TAG", "")
    captured = _capture_post(monkeypatch)
    assert REAL_SEND("hi") is True
    assert captured["text"] == "hi"


def test_send_prefix_html_escaped(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(telegram, "_WALLET_TAG", "a<b&c")
    captured = _capture_post(monkeypatch)
    assert REAL_SEND("hi") is True
    assert captured["text"] == "[a&lt;b&amp;c]\nhi"


def test_wallet_tag_derivation(monkeypatch):
    monkeypatch.setattr(telegram, "WALLET_LABEL", "")
    monkeypatch.setattr(
        telegram, "WALLET_ADDRESS",
        "0x1A1d0000000000000000000000000000000000111",
    )
    assert telegram._wallet_tag() == "0x1A1d…0111"

    monkeypatch.setattr(telegram, "WALLET_LABEL", "X")
    assert telegram._wallet_tag() == "X"

    monkeypatch.setattr(telegram, "WALLET_LABEL", "")
    monkeypatch.setattr(telegram, "WALLET_ADDRESS", "")
    assert telegram._wallet_tag() == ""
