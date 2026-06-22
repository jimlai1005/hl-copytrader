"""config 解析要去掉行內註解。
根因：systemd EnvironmentFile 不會去行內註解（python-dotenv 會），所以
`NOTIFY_VOLATILITY=true   # 說明` 在 service 裡會變成 'true   # 說明'，
若不處理 .lower()=='true' 會誤判成 False，導致波動通知在 service 永遠不發。"""
from src import config


def test_env_bool_strips_inline_comment(monkeypatch):
    monkeypatch.setenv("X_FLAG", "true     # 我的帳戶波動權重（每小時一則）")
    assert config._env_bool("X_FLAG", "false") is True
    monkeypatch.setenv("X_FLAG", "false # c")
    assert config._env_bool("X_FLAG", "true") is False


def test_env_bool_plain_values_unchanged(monkeypatch):
    monkeypatch.setenv("X_FLAG", "true")
    assert config._env_bool("X_FLAG", "false") is True
    monkeypatch.delenv("X_FLAG", raising=False)
    assert config._env_bool("X_FLAG", "true") is True       # 用 default
    assert config._env_bool("X_FLAG", "false") is False


def test_env_num_strips_inline_comment(monkeypatch):
    monkeypatch.setenv("X_NUM", "1000   # 跟單資金")
    assert config._env_float("X_NUM", "5000") == 1000.0
    monkeypatch.setenv("X_NUM", "14 # days")
    assert config._env_int("X_NUM", "14") == 14


def test_env_str_strips_inline_comment(monkeypatch):
    monkeypatch.setenv("X_STR", "mainnet   # 網路")
    assert config._env_str("X_STR", "testnet") == "mainnet"
