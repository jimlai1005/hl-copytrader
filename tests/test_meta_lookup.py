from src import trader


class FakeInfo:
    def __init__(self, universe_by_dex):
        self._u = universe_by_dex

    def meta(self, dex=""):
        return {"universe": self._u.get(dex, [])}


def _info():
    return FakeInfo({
        "": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}],
        "xyz": [{"name": "xyz:NVDA", "szDecimals": 2, "maxLeverage": 20, "onlyIsolated": True}],
    })


def test_sz_decimals():
    info = _info()
    assert trader.get_sz_decimals(info, "BTC") == 5
    assert trader.get_sz_decimals(info, "xyz:NVDA") == 2
    assert trader.get_sz_decimals(info, "UNKNOWN") == 4  # default


def test_max_leverage():
    info = _info()
    assert trader.get_max_leverage(info, "BTC") == 40
    assert trader.get_max_leverage(info, "xyz:NVDA") == 20
    assert trader.get_max_leverage(info, "UNKNOWN") == 0  # default


def test_only_isolated():
    info = _info()
    assert trader.get_only_isolated(info, "BTC") is False
    assert trader.get_only_isolated(info, "xyz:NVDA") is True
    assert trader.get_only_isolated(info, "UNKNOWN") is False  # default
