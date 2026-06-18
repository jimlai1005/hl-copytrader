from src import orders


def _order(coin, reduce_only):
    return {"coin": coin, "is_buy": False, "limit_px": 200, "trigger_px": 0,
            "size": 1.0, "reduce_only": reduce_only, "is_trigger": False,
            "tpsl": None, "is_market": False, "tif": "Gtc", "order_type_name": "Limit"}


def test_reduce_only_skipped_without_position(dry_trader):
    target_orders = [_order("ETH", reduce_only=True), _order("BTC", reduce_only=False)]
    desired, _small, _spot, _prot = orders._build_desired(
        dry_trader, target_orders, scale=1.0, protected=set(), my_positions={})
    coins = {d["coin"] for d in desired}
    assert "ETH" not in coins
    assert "BTC" in coins


def test_reduce_only_kept_with_position(dry_trader):
    from tests.conftest import make_pos
    target_orders = [_order("ETH", reduce_only=True)]
    desired, *_ = orders._build_desired(
        dry_trader, target_orders, scale=1.0, protected=set(),
        my_positions={"ETH": make_pos("ETH")})
    assert {d["coin"] for d in desired} == {"ETH"}
