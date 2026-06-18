import time
from src import orders


def _order(coin, oid=None, px=100):
    d = {"coin": coin, "is_buy": True, "limit_px": px, "trigger_px": 0, "size": 1.0,
         "reduce_only": False, "is_trigger": False, "tpsl": None, "is_market": False,
         "tif": "Gtc", "order_type_name": "Limit"}
    if oid is not None:
        d["oid"] = oid
    return d


def test_modify_skipped_after_recent_failure(dry_trader, monkeypatch):
    orders._modify_fail_until.clear()
    orders._modify_fail_until["BTC"] = time.time() + orders._MODIFY_SKIP_TTL
    called = {"modify": 0}
    monkeypatch.setattr(dry_trader, "modify_order",
                        lambda oid, spec: called.__setitem__("modify", called["modify"] + 1) or True)
    desired = [_order("BTC", px=99)]
    mine = [_order("BTC", oid=1, px=100)]
    res = orders._reconcile_orders(dry_trader, "api", "", desired, mine)
    assert called["modify"] == 0
    assert res["cancelled"] == 1 and res["placed"] == 1
