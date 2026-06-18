def test_config_strategy_defaults():
    from src import config
    assert config.VOL_LOOKBACK_DAYS == 14
    assert config.VOL_Z_SLOPE == 0.2
    assert config.VOL_Z_MAX_REDUCTION == 0.7
    assert config.HOLDING_LOOKBACK_DAYS == 14
    assert config.HOLDING_MIN_TRADES == 10


def test_modules_use_config():
    from src import config, weight, protection
    assert weight.LOOKBACK_DAYS == config.VOL_LOOKBACK_DAYS
    assert weight._Z_SLOPE == config.VOL_Z_SLOPE
    assert weight._Z_MAX_REDUCTION == config.VOL_Z_MAX_REDUCTION
    assert protection.LOOKBACK_DAYS == config.HOLDING_LOOKBACK_DAYS
    assert protection.MIN_TRADES == config.HOLDING_MIN_TRADES
