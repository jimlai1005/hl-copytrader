def test_imports():
    import main  # noqa: F401
    from src import config, monitor, orders, trader, sync, telegram, weight, protection  # noqa: F401


def test_coin_dex():
    from src.trader import _coin_dex
    assert _coin_dex("xyz:NVDA") == "xyz"
    assert _coin_dex("BTC") == ""
