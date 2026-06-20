"""回撤基準：當前與高點都取自 portfolio 的『總帳戶淨值』，
而非 perp 子帳 accountValue（unified 帳戶會少算 spot 抵押 → 假性回撤）。"""
from src import monitor


def _fake_portfolio(rows):
    def post(api_url, payload):
        assert payload["type"] == "portfolio"
        return rows
    return post


def test_account_equity_uses_total_not_perp(monkeypatch):
    # 總列(day/week)=1031；perp 列=544。應回總值、忽略 perp。
    rows = [
        ["day", {"accountValueHistory": [[1, "1000.0"], [2, "1031.2"]]}],
        ["week", {"accountValueHistory": [[1, "1000.0"], [2, "1031.2"]]}],
        ["perpWeek", {"accountValueHistory": [[1, "600.0"], [2, "544.0"]]}],
    ]
    monkeypatch.setattr(monitor, "_post", _fake_portfolio(rows))
    current, peak = monitor.get_account_equity("api", "0xabc")
    assert current == 1031.2
    assert peak == 1031.2  # 持平 → 0% 回撤（重現 bug 場景：帳戶其實沒虧）


def test_account_equity_detects_real_drawdown(monkeypatch):
    rows = [
        ["day", {"accountValueHistory": [[1, "1000.0"], [3, "700.0"]]}],
        ["week", {"accountValueHistory": [[1, "1000.0"], [2, "950.0"], [3, "700.0"]]}],
    ]
    monkeypatch.setattr(monitor, "_post", _fake_portfolio(rows))
    current, peak = monitor.get_account_equity("api", "0xabc")
    assert current == 700.0   # 最新時間戳 ts=3
    assert peak == 1000.0     # week 區間最大
    assert (peak - current) / peak == 0.3


def test_account_equity_network_fail_is_safe(monkeypatch):
    def boom(api_url, payload):
        raise RuntimeError("net down")
    monkeypatch.setattr(monitor, "_post", boom)
    # 取不到 → (0,0)，呼叫端 peak<=0 會跳過回撤判斷，不會誤觸停單
    assert monitor.get_account_equity("api", "0xabc") == (0.0, 0.0)
