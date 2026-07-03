"""下單回應錯誤解析：Hyperliquid 頂層錯誤(限流/簽名)回 {'status':'err','response':'<字串>'}，
response 是 str 不是 dict。過去 _extract_order_error 對它 .get() → 'str' object has no attribute 'get'。
限流錯誤訊息含會變動的數字，需用固定 dedup key 避免洗版告警。"""
from src.instrument import _extract_order_error, _route_order_error
from src import telegram

RATE_LIMIT = ("Too many cumulative requests sent (43645 > 40140) for cumulative "
              "volume traded $30141.76. Place taker orders to free up 1 request per USDC traded.")


def test_top_level_err_string_response():
    r = {"status": "err", "response": RATE_LIMIT}
    assert _extract_order_error(r) == RATE_LIMIT          # 不再 crash，回傳字串本身


def test_order_level_error_in_statuses():
    r = {"status": "ok", "response": {"type": "order",
         "data": {"statuses": [{"error": "Insufficient margin"}]}}}
    assert _extract_order_error(r) == "Insufficient margin"


def test_success_no_error():
    r = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}
    assert _extract_order_error(r) == ""


def test_non_dict_or_missing_response():
    assert _extract_order_error(None) == ""
    assert _extract_order_error("weird") == ""
    assert _extract_order_error({"status": "ok"}) == ""


def test_rate_limit_uses_stable_dedup_key(monkeypatch):
    keys = []
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: keys.append(k.get("dedup_key")))
    # 不同 coin、訊息數字不同，但限流一律用固定 dedup key → 才能被去重、不洗版
    _route_order_error("xyz:SPCX", RATE_LIMIT, 100, "掛單")
    _route_order_error("xyz:TSLA",
                       "Too many cumulative requests sent (43700 > 40140) ...", 100, "掛單")
    assert keys == ["rate_limited", "rate_limited"]


def test_non_rate_limit_error_routes_to_api_error(monkeypatch):
    keys = []
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: keys.append(k.get("dedup_key")))
    _route_order_error("BTC", "Some other failure", 100, "掛單")
    assert keys and keys[0].startswith("apierr:")
