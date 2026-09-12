# Isolated 槓桿跟目標 + 清算冷卻 + 關鍵告警重試 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 xyz（isolated）標的的名目槓桿跟隨目標交易員（無目標部位時預設 4x），被清算的標的進入 2 小時冷卻不重開，回撤／停機告警失敗時重試。

**Architecture:** 三個獨立小改動，共用同一分支。(1) `Trader.entry_leverage` 多收 `target_leverage`，isolated 時用它、cross 維持 max；呼叫端（`sync.py` 安全網、`orders.py` 掛單）把目標該標的的槓桿傳進來。(2) 新模組 `src/liquidation.py` 從 `userFillsByTime` 的 `liquidation.liquidatedUser` 欄位偵測我方被清算，回傳 `{coin: 冷卻截止 unix 秒}`；`orders.sync_open_orders` 把它與抗單保護集合聯集後傳給既有的「只准減倉」跳過邏輯（不新增第二套跳過機制）。(3) `telegram._send` 多收 `retries`，`alert_drawdown`／`alert_bot_stopped`／新的 `alert_liquidated` 用 `retries=2`。

**Tech Stack:** Python 3.9、pytest（`python3 -m pytest tests/ -q`，基線 105 passed）、Hyperliquid `/info` REST。

**背景（給實作者）：** 30 天內我方被清算 20 次全在 xyz，因為 xyz 只能 isolated、而程式一律設最大槓桿（CL 20x → 清算緩衝約 2.5%；目標同標的用 4x）。清算後安全網看到「目標有、我沒有」1 分鐘內就市價重開，再被清算。三個修法的決策已與使用者確認：isolated 跟目標槓桿、無目標部位預設 4x；冷卻 2 小時；告警重試。**10-8（安全網觸發改為目標 size 變動）刻意不在本 plan 內，不要順手做。**

**紅線：** 本 plan 只改程式與測試，**不得**碰伺服器、不得執行 `deploy/*.sh`、不得重啟 service、不得改 `.env`。`.env` 內有真鑰匙，不要印出來。

**分支：** `fix/isolated-leverage-liquidation-cooldown`（從 `main` 開，HEAD `f2220f9`）。

---

## 檔案地圖

| 檔案 | 動作 | 責任 |
|---|---|---|
| `src/config.py` | 修改 | 新增 `ISOLATED_ORDER_LEVERAGE`（預設 4）、`LIQUIDATION_COOLDOWN_HOURS`（預設 2） |
| `src/trader.py:95-108` | 修改 | `entry_leverage(coin, target_leverage=0)`：isolated 用目標槓桿或預設 4x |
| `src/sync.py:121-126` | 修改 | 安全網把 `tgt_pos["leverage"]` 傳給 `entry_leverage` |
| `src/orders.py:79-127, 187-193, 308-331` | 修改 | desired spec 帶 `target_leverage`；`_set_entry_leverage` 使用它；接入清算冷卻集合 |
| `src/liquidation.py` | 新建 | 清算偵測＋冷卻表（含快取、失敗安全預設） |
| `src/telegram.py:25-51, 241-249, 324-329` | 修改 | `_send(retries=)`、`alert_liquidated`、關鍵告警加重試 |
| `.env.example` | 修改 | 兩個新參數說明 |
| `tests/test_isolated_leverage.py` | 新建 | Task 1、2 |
| `tests/test_liquidation_cooldown.py` | 新建 | Task 3、4 |
| `tests/test_alert_retry.py` | 新建 | Task 5 |

---

### Task 0: 開分支 `@sdd`

- [ ] **Step 1: 建分支**

```bash
cd /Users/jim/projects/hl-copytrader
git checkout -b fix/isolated-leverage-liquidation-cooldown main
python3 -m pytest tests/ -q 2>&1 | tail -1
```
Expected: `105 passed`

---

### Task 1: `entry_leverage` 支援 isolated 跟目標槓桿 `@inline`

**Files:**
- Modify: `src/config.py`（在 `ORDER_LEVERAGE` 定義之後，第 112 行後）
- Modify: `src/trader.py:95-108`
- Test: `tests/test_isolated_leverage.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_isolated_leverage.py
"""isolated 標的的名目槓桿：跟目標槓桿；目標無部位時用 ISOLATED_ORDER_LEVERAGE。
cross 標的維持既有行為（ORDER_LEVERAGE=max → 標的最大槓桿）。

為何：isolated 部位的保證金＝名目/槓桿，槓桿直接決定清算價；
一律 max 讓 xyz 標的在 2.5%~5% 反向走勢就被清算（30 天 20 次），目標自己用 3~4x。"""
import pytest

from src import trader as trader_mod
from src.trader import Trader


class FakeInfo:
    def meta(self, dex=""):
        if dex == "xyz":
            return {"universe": [
                {"name": "xyz:CL", "szDecimals": 3, "maxLeverage": 20},
                {"name": "xyz:MINIMAX", "szDecimals": 2, "maxLeverage": 10, "onlyIsolated": True},
            ]}
        return {"universe": [
            {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
            {"name": "PONS", "szDecimals": 0, "maxLeverage": 3, "onlyIsolated": True},
        ]}


@pytest.fixture
def t(monkeypatch):
    monkeypatch.setattr(trader_mod, "ORDER_LEVERAGE", "max")
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    return Trader(None, FakeInfo(), live_trading=False)


def test_cross_keeps_max(t):
    assert t.entry_leverage("BTC") == 40


def test_cross_ignores_target_leverage(t):
    assert t.entry_leverage("BTC", target_leverage=5) == 40


def test_isolated_default_when_no_target_position(t):
    assert t.entry_leverage("xyz:CL") == 4


def test_isolated_follows_target_leverage(t):
    assert t.entry_leverage("xyz:CL", target_leverage=3) == 3


def test_isolated_target_leverage_clamped_to_max(t):
    assert t.entry_leverage("xyz:MINIMAX", target_leverage=50) == 10


def test_isolated_default_clamped_to_max(t, monkeypatch):
    # PONS 上限 3x < 預設 4x → 夾到 3
    assert t.entry_leverage("PONS") == 3


def test_only_isolated_on_default_dex_uses_isolated_rule(t):
    # PONS 是預設 dex 但 onlyIsolated → 走 isolated 規則（跟目標）
    assert t.entry_leverage("PONS", target_leverage=2) == 2


def test_isolated_without_info_uses_default(monkeypatch):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    t = Trader(None, None, live_trading=False)
    assert t.entry_leverage("xyz:CL") == 4          # 無 info：xyz 靠前綴判 isolated
    assert t.entry_leverage("xyz:CL", target_leverage=6) == 6
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_isolated_leverage.py -q`
Expected: FAIL（`ISOLATED_ORDER_LEVERAGE` 不存在 / `target_leverage` 是未知參數）

- [ ] **Step 3: config 新增參數**

在 `src/config.py` 第 112 行 `ORDER_LEVERAGE = ...` 之後加：

```python
# isolated 標的（xyz 美股、onlyIsolated 資產）的名目槓桿：
# isolated 的保證金＝名目/槓桿，槓桿直接決定清算價，不能像 cross 一樣設 max。
# 目標在該標的有部位時一律跟目標的槓桿；沒有部位（純掛單）時用此預設值。
# 30 天內 20 次被清算的部位在 4x 下全部存活（最大反向 17.9% < 4x 緩衝 21%）。
ISOLATED_ORDER_LEVERAGE = _env_int("ISOLATED_ORDER_LEVERAGE", "4")
if ISOLATED_ORDER_LEVERAGE < 1:
    ISOLATED_ORDER_LEVERAGE = 4
```

- [ ] **Step 4: 改 `entry_leverage`**

`src/trader.py` 第 10 行的 import 改成：
```python
from .config import ORDER_LEVERAGE, MIN_ORDER_NOTIONAL, ISOLATED_ORDER_LEVERAGE
```

把 `src/trader.py:95-108` 整個 `entry_leverage` 換成：

```python
    def entry_leverage(self, coin: str, target_leverage: int = 0) -> int:
        """
        進場單/部位要設定的名目槓桿。
        cross 標的：ORDER_LEVERAGE="max" 用標的最大槓桿（掛單/部位佔用保證金＝名目/槓桿，
          cross 下整個帳戶都是保證金，設高只省保證金、不影響清算價）。
        isolated 標的（xyz、onlyIsolated）：保證金只有名目/槓桿，槓桿直接決定清算價，
          所以跟目標在該標的的槓桿 target_leverage；目標沒有部位（純掛單）時用
          ISOLATED_ORDER_LEVERAGE。兩者都夾到標的上限。
        """
        max_lev = self._get_max_leverage(coin)  # 0 = 未知（如 dry-run 無 info）
        if not self.entry_is_cross(coin):
            want = int(target_leverage) if target_leverage and target_leverage > 0 else ISOLATED_ORDER_LEVERAGE
            return max(1, min(want, max_lev) if max_lev > 0 else want)
        if ORDER_LEVERAGE == "max":
            return max(1, max_lev if max_lev > 0 else ENTRY_LEVERAGE_FALLBACK)
        try:
            want = int(ORDER_LEVERAGE)
        except ValueError:
            want = ENTRY_LEVERAGE_FALLBACK
        return max(1, min(want, max_lev) if max_lev > 0 else want)
```

注意 `entry_is_cross` 在 `info is None` 時對預設 dex 回 True（cross）、對 `xyz:` 前綴回 False，所以無 info 的 dry-run 對 xyz 也會走 isolated 規則（測試 `test_isolated_without_info_uses_default` 覆蓋）。

- [ ] **Step 5: 跑測試確認通過＋全量回歸**

Run: `python3 -m pytest tests/test_isolated_leverage.py -q && python3 -m pytest tests/ -q | tail -1`
Expected: 新檔 8 passed；全量 `113 passed`。若 `tests/test_live_execution.py` 有斷言 xyz 標的 `update_leverage` 被呼叫的倍率（例如 20），那是 characterization 測試：把期望值改成新規則下的值（`xyz:NVDA` maxLeverage 20、無目標槓桿 → 4）並在該測試加一行註解說明原因。**不要改動 cross 標的的期望值。**

- [ ] **Step 6: Commit**

```bash
git add src/config.py src/trader.py tests/test_isolated_leverage.py tests/test_live_execution.py
git commit -m "feat(trader): isolated coins follow target leverage, default ISOLATED_ORDER_LEVERAGE=4

Isolated margin = notional/leverage, so max leverage on xyz meant 2.5-5%
liquidation buffer (20 liquidations in 30d). Cross behaviour unchanged."
```

---

### Task 2: 安全網與掛單把目標槓桿傳進去 `@inline`

**Files:**
- Modify: `src/sync.py:121-126`
- Modify: `src/orders.py:79-127`（`_build_desired`）、`src/orders.py:187-193`（`_set_entry_leverage`）、`src/orders.py:314-316`（呼叫 `_build_desired`）
- Test: `tests/test_isolated_leverage.py`（追加）

- [ ] **Step 1: 追加失敗測試**

在 `tests/test_isolated_leverage.py` 末尾追加：

```python
# ── 呼叫端接線 ──────────────────────────────────────────────
from src import sync, orders


def _tgt_pos(coin, size, leverage, lev_type="isolated", side="long"):
    return {"coin": coin, "dex": "xyz" if ":" in coin else "", "side": side, "size": size,
            "entry_px": 100.0, "leverage": leverage, "leverage_type": lev_type,
            "notional": size * 100.0, "unrealized_pnl": 0.0}


def test_safety_net_open_passes_target_leverage(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    seen = {}
    def fake_open(coin, is_buy, size, leverage, is_cross, **kw):
        seen.update(coin=coin, leverage=leverage, is_cross=is_cross)
        return {"status": "dry_run"}
    monkeypatch.setattr(dry_trader, "open_position", fake_open)
    target_state = {"account_value": 1000.0, "failed_dexs": set(),
                    "positions": {"xyz:CL": _tgt_pos("xyz:CL", 500.0, 4, side="short")}}
    my_state = {"account_value": 1000.0, "positions": {}}
    sync.sync_positions("api", dry_trader, target_state, my_state, scale=0.002)
    assert seen == {"coin": "xyz:CL", "leverage": 4, "is_cross": False}


def test_safety_net_open_uses_target_leverage_3(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:MINIMAX"] = 2
    seen = {}
    monkeypatch.setattr(dry_trader, "open_position",
                        lambda coin, is_buy, size, leverage, is_cross, **kw: seen.update(leverage=leverage) or {"status": "dry_run"})
    target_state = {"account_value": 1000.0, "failed_dexs": set(),
                    "positions": {"xyz:MINIMAX": _tgt_pos("xyz:MINIMAX", 2700.0, 3)}}
    sync.sync_positions("api", dry_trader, target_state, {"account_value": 1000.0, "positions": {}}, scale=0.001)
    assert seen["leverage"] == 3


def _tgt_order(coin, size=1000.0, px=100.0, reduce_only=False, is_buy=True):
    return {"coin": coin, "is_buy": is_buy, "size": size, "limit_px": px, "trigger_px": 0.0,
            "reduce_only": reduce_only, "is_trigger": False, "tpsl": None,
            "is_market": False, "tif": "Gtc", "order_type_name": "Limit"}


def test_build_desired_carries_target_leverage(dry_trader):
    dry_trader._sz_dec["xyz:CL"] = 3
    target_positions = {"xyz:CL": _tgt_pos("xyz:CL", 500.0, 4, side="short")}
    desired, *_ = orders._build_desired(
        dry_trader, [_tgt_order("xyz:CL"), _tgt_order("BTC")], scale=0.01,
        protected=set(), my_positions={}, target_positions=target_positions,
    )
    by_coin = {d["coin"]: d for d in desired}
    assert by_coin["xyz:CL"]["target_leverage"] == 4
    assert by_coin["BTC"]["target_leverage"] == 0       # 目標無部位 → 0


def test_set_entry_leverage_uses_spec_target_leverage(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    calls = []
    monkeypatch.setattr(dry_trader, "set_leverage", lambda coin, lev, cross: calls.append((coin, lev, cross)) or True)
    orders._set_entry_leverage(dry_trader, {"coin": "xyz:CL", "reduce_only": False, "target_leverage": 6})
    orders._set_entry_leverage(dry_trader, {"coin": "xyz:CL", "reduce_only": False, "target_leverage": 0})
    orders._set_entry_leverage(dry_trader, {"coin": "xyz:CL", "reduce_only": False})   # 舊 spec 無欄位
    assert calls == [("xyz:CL", 6, False), ("xyz:CL", 4, False), ("xyz:CL", 4, False)]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_isolated_leverage.py -q`
Expected: 新增 4 個 FAIL（`_build_desired` 不認 `target_positions`、spec 無 `target_leverage`、安全網傳的是 max）

- [ ] **Step 3: 改 `sync.py`**

`src/sync.py:124-125` 改成：
```python
        # 名目槓桿：cross 用標的最大值；isolated（xyz/onlyIsolated）跟目標該標的的槓桿
        leverage = trader.entry_leverage(coin, tgt_pos.get("leverage", 0))
```

- [ ] **Step 4: 改 `orders.py`**

`_build_desired` 簽章與 docstring（`src/orders.py:79-90`）改成：
```python
def _build_desired(trader: Trader, target_orders: list, scale: float,
                   protected: set = None, my_positions: dict = None,
                   target_positions: dict = None) -> tuple:
    """
    將目標掛單縮放成「我方期望掛單規格」清單。
    回傳 (desired_specs, skipped_small, skipped_spot, skipped_protected)。
      - skipped_small：名目值過小被跳過的 [(coin, notional)]
      - skipped_spot：現貨標的（不支援跟單）被跳過的 [coin]
      - skipped_protected：保護（抗單/清算冷卻）下拒絕補倉(非 reduce-only)被跳過的 [coin]
    現貨單先排除避免下單錯誤；受保護標的的補倉單(非 reduce-only)排除、但保留其減倉/止盈止損單。
    每張 spec 帶 target_leverage（目標在該標的的部位槓桿，無部位=0），供 isolated 標的設槓桿用。
    """
    protected = protected or set()
    my_positions = my_positions or {}
    target_positions = target_positions or {}
```
在 `desired.append({...})` 的 dict 最後一個欄位 `"order_type_name": o["order_type_name"],` 之後加一行：
```python
            "target_leverage": (target_positions.get(coin) or {}).get("leverage", 0),
```

`_set_entry_leverage`（`src/orders.py:187-193`）換成：
```python
def _set_entry_leverage(trader: Trader, desired: dict) -> None:
    """進場單（非 reduce-only）下單前設定名目槓桿。cross 用 max 最省保證金；
    xyz/onlyIsolated 資產自動改 isolated，且槓桿跟目標（spec 的 target_leverage，無則預設）。"""
    if desired["reduce_only"]:
        return
    coin = desired["coin"]
    trader.set_leverage(
        coin,
        trader.entry_leverage(coin, desired.get("target_leverage", 0)),
        trader.entry_is_cross(coin),
    )
```

`sync_open_orders` 裡呼叫 `_build_desired`（`src/orders.py:314-316`）改成：
```python
    desired, skipped_small, skipped_spot, skipped_protected = _build_desired(
        trader, target_orders, scale, set(protected), my_state.get("positions", {}),
        target_positions,
    )
```

`_orders_match`（`orders.py:46-76`）只比對明確欄位，多出的 `target_leverage` 不影響配對；不要動它。

- [ ] **Step 5: 跑測試＋全量回歸**

Run: `python3 -m pytest tests/test_isolated_leverage.py -q && python3 -m pytest tests/ -q | tail -1`
Expected: 新檔 12 passed；全量 `117 passed`

- [ ] **Step 6: 本機乾跑（唯讀，不下單）**

Run: `python3 main.py --once --dry-run 2>&1 | grep -E "lev=|設定 .* 槓桿" | head -20`
Expected: xyz 標的顯示 `lev=3x`／`lev=4x`（有目標部位者等於目標槓桿），BTC/HYPE 等仍是 `40x`／`10x` 之類的上限值。把輸出前 20 行貼進回報。

- [ ] **Step 7: Commit**

```bash
git add src/sync.py src/orders.py tests/test_isolated_leverage.py
git commit -m "feat(sync,orders): pass target position leverage to entry_leverage for isolated coins"
```

---

### Task 3: 清算偵測模組 `src/liquidation.py` `@inline`

**Files:**
- Modify: `src/config.py`（`ISOLATED_ORDER_LEVERAGE` 之後）
- Create: `src/liquidation.py`
- Test: `tests/test_liquidation_cooldown.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_liquidation_cooldown.py
"""清算冷卻：從 userFillsByTime 的 liquidation.liquidatedUser 欄位偵測我方被清算，
該標的進入 LIQUIDATION_COOLDOWN_HOURS 冷卻（只准減倉、不新開不加倉、不掛補倉單）。

為何：安全網無記憶，被清算後看到「目標有、我沒有」1 分鐘內就市價重開，30 天內
「清算後 5 分鐘內重開」23 筆，CL 在 6 小時內被清算兩次。"""
import time

from src import liquidation as liq

ME = "0x1A1d5eF3256e1A7de2db2082D7A1eEb976c90111"
NOW = 1_800_000_000.0   # 固定「現在」(秒)


def _fill(coin, t_sec, liquidated_user=None, pnl="-5.0"):
    f = {"coin": coin, "time": int(t_sec * 1000), "closedPnl": pnl, "side": "B", "sz": "1", "px": "100"}
    if liquidated_user:
        f["liquidation"] = {"liquidatedUser": liquidated_user, "markPx": "100", "method": "market"}
    return f


def _install(monkeypatch, fills, hours=2.0):
    calls = {"n": 0}
    def post(api_url, payload):
        calls["n"] += 1
        assert payload["type"] == "userFillsByTime"
        assert payload["user"] == ME
        return fills
    monkeypatch.setattr(liq, "_post", post)
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", hours)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}})
    monkeypatch.setattr(liq, "_alerted", set())      # 模組級集合，測試間必須重置
    return calls


def test_my_liquidation_enters_cooldown(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 1800, ME.lower())])
    out = liq.get_liquidation_cooldowns("api", ME, now=NOW)
    assert set(out) == {"xyz:CL"}
    assert out["xyz:CL"] == (NOW - 1800) + 2 * 3600


def test_counterparty_liquidation_is_ignored(monkeypatch):
    _install(monkeypatch, [_fill("HYPE", NOW - 60, "0x000000000000000000000000000000000000dead")])
    assert liq.get_liquidation_cooldowns("api", ME, now=NOW) == {}


def test_normal_fill_is_ignored(monkeypatch):
    _install(monkeypatch, [_fill("HYPE", NOW - 60)])
    assert liq.get_liquidation_cooldowns("api", ME, now=NOW) == {}


def test_expired_cooldown_dropped(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 3 * 3600, ME)])
    assert liq.get_liquidation_cooldowns("api", ME, now=NOW) == {}


def test_latest_liquidation_wins(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 5000, ME), _fill("xyz:CL", NOW - 100, ME)])
    out = liq.get_liquidation_cooldowns("api", ME, now=NOW)
    assert out["xyz:CL"] == (NOW - 100) + 2 * 3600


def test_cached_within_ttl(monkeypatch):
    calls = _install(monkeypatch, [_fill("xyz:CL", NOW - 100, ME)])
    liq.get_liquidation_cooldowns("api", ME, now=NOW)
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 10)
    assert calls["n"] == 1


def test_fetch_failure_keeps_previous_flags(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 100, ME)])
    first = liq.get_liquidation_cooldowns("api", ME, now=NOW)
    def boom(api_url, payload):
        raise RuntimeError("net down")
    monkeypatch.setattr(liq, "_post", boom)
    liq._cache["ts"] = 0.0                      # 讓快取失效，強迫重抓
    again = liq.get_liquidation_cooldowns("api", ME, now=NOW + 120)
    assert again == first                       # 失敗 → 沿用上次結果，不是空集合


def test_new_liquidation_alerts_once(monkeypatch):
    _install(monkeypatch, [_fill("xyz:CL", NOW - 100, ME, pnl="-9.52")])
    sent = []
    from src import telegram
    monkeypatch.setattr(telegram, "alert_liquidated", lambda coin, pnl, until: sent.append((coin, pnl)))
    liq.get_liquidation_cooldowns("api", ME, now=NOW)
    liq._cache["ts"] = 0.0
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 120)   # 同一筆再看到 → 不重發
    assert sent == [("xyz:CL", -9.52)]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_liquidation_cooldown.py -q`
Expected: FAIL（`src.liquidation` 不存在）

- [ ] **Step 3: config 新增參數**

`src/config.py` 在 `ISOLATED_ORDER_LEVERAGE` 區塊之後加：
```python
# 清算冷卻（小時）：我方某標的被交易所清算後，此期間內該標的只准減倉/平倉，
# 不新開、不加倉、不掛補倉單，避免安全網在急拉後立刻市價重開又被清算。0=停用。
LIQUIDATION_COOLDOWN_HOURS = _env_float("LIQUIDATION_COOLDOWN_HOURS", "2")
if LIQUIDATION_COOLDOWN_HOURS < 0:
    LIQUIDATION_COOLDOWN_HOURS = 2.0
```

- [ ] **Step 4: 新建 `src/liquidation.py`**

```python
"""
清算冷卻模組。

從我方 userFillsByTime 找 `liquidation.liquidatedUser == 我方地址` 的成交（＝我被交易所清算），
該標的進入 LIQUIDATION_COOLDOWN_HOURS 冷卻：orders/sync 對它只准減倉/平倉、不新開不加倉、
不掛補倉單（沿用抗單保護的 protected 集合機制）。

為何：安全網沒有記憶，被清算後看到「目標有、我沒有」會 1 分鐘內市價重開，
在剛把我清掉的急拉後重進場，是第二次被清算機率最高的時點。

設計：
- 不需要狀態檔：冷卻表每次從成交紀錄重建（只看最近 COOLDOWN_HOURS），重啟後自然延續。
- 快取 60 秒；抓取失敗時沿用上次結果（安全預設是「維持保護」，不是「解除保護」）。
- 新出現的清算發一則 Telegram（alert_liquidated，帶 dedup key，一律發送）。
"""
import logging
import time as _time
from datetime import datetime

from .config import LIQUIDATION_COOLDOWN_HOURS
from .monitor import _post
from . import telegram as tg

logger = logging.getLogger(__name__)

COOLDOWN_HOURS = LIQUIDATION_COOLDOWN_HOURS
_CACHE_TTL = 60

# until: coin -> 冷卻截止 unix 秒；seen: 已告警的 (coin, 清算時間 ms)
_cache = {"ts": 0.0, "address": None, "until": {}}
_alerted = set()


def _my_liquidations(api_url: str, address: str, since_ms: int) -> list:
    """回傳 [(coin, time_ms, closed_pnl)]，只含「我方是被清算方」的成交。"""
    fills = _post(api_url, {"type": "userFillsByTime", "user": address, "startTime": since_ms})
    me = address.lower()
    out = []
    for f in fills:
        liq = f.get("liquidation")
        if not liq or str(liq.get("liquidatedUser", "")).lower() != me:
            continue
        out.append((f["coin"], int(f["time"]), float(f.get("closedPnl", 0) or 0)))
    return out


def get_liquidation_cooldowns(api_url: str, address: str, now: float = None) -> dict:
    """
    回傳 {coin: 冷卻截止 unix 秒}，只含仍在冷卻中的標的。
    COOLDOWN_HOURS <= 0 或 address 空 → {}。
    """
    if COOLDOWN_HOURS <= 0 or not address:
        return {}
    now = _time.time() if now is None else now
    if _cache["address"] == address and now - _cache["ts"] < _CACHE_TTL:
        return {c: u for c, u in _cache["until"].items() if u > now}

    window_s = COOLDOWN_HOURS * 3600
    try:
        events = _my_liquidations(api_url, address, int((now - window_s) * 1000))
    except Exception as e:
        logger.warning(f"取得清算紀錄失敗，沿用上次冷卻表: {e}")
        return {c: u for c, u in _cache["until"].items() if u > now}

    until = {}
    for coin, t_ms, pnl in events:
        end = t_ms / 1000 + window_s
        if end <= now:
            continue
        if end > until.get(coin, 0):
            until[coin] = end
        key = (coin, t_ms)
        if key not in _alerted:
            _alerted.add(key)
            until_str = datetime.fromtimestamp(end).strftime("%m/%d %H:%M")
            logger.warning(f"[清算冷卻] {coin} 於 {datetime.fromtimestamp(t_ms/1000):%m/%d %H:%M} 被清算"
                           f"（已實現 {pnl:+.2f}），冷卻至 {until_str}，期間只准減倉")
            tg.alert_liquidated(coin, pnl, until_str)

    _cache.update(ts=now, address=address, until=until)
    return dict(until)
```

- [ ] **Step 5: `telegram.py` 新增 `alert_liquidated`**

在 `src/telegram.py` 的 `alert_bot_stopped` 之前加（`retries` 參數由 Task 5 才實作；本 task 先不帶 `retries`，Task 5 再補）：
```python
# ── 被清算告警（一律發送）──────────────────────────────────
def alert_liquidated(coin: str, pnl: float, until_str: str) -> None:
    _send(
        f"【警告】{_c(coin)} 部位被交易所清算 💥\n"
        f"<b>時間：</b>{_now()}\n"
        f"<b>已實現損益：</b>{pnl:+.2f} USDC\n"
        f"<b>冷卻至：</b>{until_str}（期間只准減倉，不重開不加倉）",
        dedup_key=f"liquidated:{coin}:{until_str}",
    )
```
`_c` 是本檔既有的幣名格式化 helper（第 58 行）。

- [ ] **Step 6: 跑測試**

Run: `python3 -m pytest tests/test_liquidation_cooldown.py -q && python3 -m pytest tests/ -q | tail -1`
Expected: 8 passed；全量 `125 passed`

- [ ] **Step 7: Commit**

```bash
git add src/config.py src/liquidation.py src/telegram.py tests/test_liquidation_cooldown.py
git commit -m "feat(liquidation): detect own liquidations from fills, per-coin cooldown table + alert"
```

---

### Task 4: 冷卻接進掛單對帳與安全網 `@inline`

**Files:**
- Modify: `src/orders.py:20-28`（import）、`src/orders.py:308-350`（`sync_open_orders`）
- Modify: `src/sync.py:143-147, 166-169`（log 文案改成通用「保護」）
- Test: `tests/test_liquidation_cooldown.py`（追加）

- [ ] **Step 1: 追加失敗測試**

```python
# 追加到 tests/test_liquidation_cooldown.py 末尾
from src import orders, sync


def _tgt_order(coin, reduce_only=False):
    return {"coin": coin, "is_buy": True, "size": 1000.0, "limit_px": 100.0, "trigger_px": 0.0,
            "reduce_only": reduce_only, "is_trigger": False, "tpsl": None,
            "is_market": False, "tif": "Gtc", "order_type_name": "Limit"}


def test_sync_open_orders_blocks_cooling_coin_orders(monkeypatch, dry_trader):
    dry_trader._sz_dec["xyz:CL"] = 3
    monkeypatch.setattr(orders, "get_liquidation_cooldowns", lambda api, addr: {"xyz:CL": NOW + 3600})
    monkeypatch.setattr(orders, "sync_positions", lambda **k: {"actions": []})
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    seen = {}
    def fake_reconcile(trader, api_url, my_address, desired, my_orders):
        seen["coins"] = [d["coin"] for d in desired]
        return {"placed": 0, "cancelled": 0, "modified": 0, "matched": 0, "sync_failed": False}
    monkeypatch.setattr(orders, "_reconcile_orders", fake_reconcile)
    target_state = {"account_value": 1000.0, "positions": {}, "failed_dexs": set()}
    my_state = {"account_value": 1000.0, "positions": {"xyz:CL": {"side": "short", "size": 1.0}}}
    orders.sync_open_orders("api", dry_trader, target_state, my_state,
                            target_orders=[_tgt_order("xyz:CL"), _tgt_order("xyz:CL", reduce_only=True), _tgt_order("BTC")],
                            my_orders=[], my_address=ME)
    # 冷卻中的 CL：補倉單被擋、reduce-only 保留；BTC 不受影響
    assert seen["coins"] == ["xyz:CL", "BTC"]
    assert dry_trader._sz_dec  # sanity


def test_sync_open_orders_passes_cooldown_to_safety_net(monkeypatch, dry_trader):
    monkeypatch.setattr(orders, "get_liquidation_cooldowns", lambda api, addr: {"xyz:CL": NOW + 3600})
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    monkeypatch.setattr(orders, "_reconcile_orders",
                        lambda *a, **k: {"placed": 0, "cancelled": 0, "modified": 0, "matched": 0, "sync_failed": False})
    got = {}
    monkeypatch.setattr(orders, "sync_positions", lambda **k: got.update(protected=k["protected"]) or {"actions": []})
    target_state = {"account_value": 1000.0, "positions": {}, "failed_dexs": set()}
    orders.sync_open_orders("api", dry_trader, target_state, {"account_value": 1000.0, "positions": {}},
                            target_orders=[], my_orders=[], my_address=ME)
    assert got["protected"] == {"xyz:CL"}


def test_dry_run_without_address_skips_cooldown_fetch(monkeypatch, dry_trader):
    called = {"n": 0}
    monkeypatch.setattr(orders, "get_liquidation_cooldowns", lambda api, addr: called.__setitem__("n", called["n"] + 1) or {})
    monkeypatch.setattr(orders, "sync_positions", lambda **k: {"actions": []})
    monkeypatch.setattr(orders, "_reconcile_orders",
                        lambda *a, **k: {"placed": 0, "cancelled": 0, "modified": 0, "matched": 0, "sync_failed": False})
    target_state = {"account_value": 1000.0, "positions": {}, "failed_dexs": set()}
    orders.sync_open_orders("api", dry_trader, target_state, {"account_value": 1000.0, "positions": {}},
                            target_orders=[], my_orders=[], my_address="")
    assert called["n"] == 0


def test_safety_net_cooling_coin_no_reopen_but_close_allowed(monkeypatch, dry_trader):
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    opened = []; closed = []
    monkeypatch.setattr(dry_trader, "open_position", lambda *a, **k: opened.append(a) or {"status": "dry_run"})
    monkeypatch.setattr(dry_trader, "close_position", lambda *a, **k: closed.append(a) or {"status": "dry_run"})
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "short", "size": 500.0, "entry_px": 100.0,
           "leverage": 4, "leverage_type": "isolated", "notional": 50000.0, "unrealized_pnl": 0.0}
    # 情境 1：目標有、我沒有、CL 冷卻中 → 不開
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                        {"account_value": 1000.0, "positions": {}}, protected={"xyz:CL"}, scale=0.002)
    assert opened == []
    # 情境 2：目標已平、我還有、CL 冷卻中 → 照平（冷卻只擋開/加倉）
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {}},
                        {"account_value": 1000.0, "positions": {"xyz:CL": {"side": "short", "size": 1.0, "unrealized_pnl": 0.0}}},
                        protected={"xyz:CL"}, scale=0.002)
    assert len(closed) == 1
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_liquidation_cooldown.py -q`
Expected: 前 2 個接線測試 FAIL（`orders` 沒有 `get_liquidation_cooldowns`）；後 2 個可能已 PASS（沿用既有 protected 機制）— 這是預期的。

- [ ] **Step 3: 改 `orders.py`**

import 區（`src/orders.py:27`）之後加：
```python
from .liquidation import get_liquidation_cooldowns
```

`sync_open_orders` 中 `protected` 計算之後（原 308-311 行）改成：
```python
    # 抗單保護（預設關閉）：偵測目標持倉時間 Z-Score 異常的標的，拒絕補倉
    protected = {}
    if HOLDING_PROTECTION_ENABLED:
        protected = get_anti_holding_flags(TARGET_TRADER, target_positions)

    # 清算冷卻：我方剛被清算的標的，冷卻期內只准減倉（不新開、不加倉、不掛補倉單）。
    # dry-run 無地址 → 不查。與抗單保護共用同一套「只准減倉」跳過機制。
    cooling = get_liquidation_cooldowns(api_url, my_address) if my_address else {}
    for coin, until in cooling.items():
        logger.warning(f"[清算冷卻] {coin} 冷卻至 {time.strftime('%m/%d %H:%M', time.localtime(until))}，本輪只准減倉")
    blocked = set(protected) | set(cooling)
```

把 `_build_desired(...)` 呼叫的 `set(protected)` 改成 `blocked`；`skipped_protected` 的告警迴圈改成：
```python
    for coin in set(skipped_protected):
        if coin in cooling:
            logger.info(f"[清算冷卻] 略過 {coin} 補倉單")
            continue
        z = protected.get(coin, 0)
        logger.warning(f"[抗單保護] 拒絕複製 {coin} 補倉單 (持倉時間 Z={z:.1f})")
        tg.notify_holding_protection(coin, z)
```
`sync_positions(...)` 呼叫的 `protected=set(protected)` 改成 `protected=blocked`。

- [ ] **Step 4: 改 `sync.py` 文案**

`src/sync.py:98` docstring 的 `protected：抗單保護標的集合；` 改成 `protected：受保護標的集合（抗單保護、清算冷卻）；`。
`src/sync.py:146` 改成：
```python
                logger.warning(f"[保護] {coin} 抗單/清算冷卻中，跳過新開倉")
```
`src/sync.py:168` 改成：
```python
                logger.warning(f"[保護] {coin} 抗單/清算冷卻中，跳過加倉（{my_size:.4f}→{target_size:.4f}）")
```
（`grep -rn "抗單" tests/` 應為空，改文案不影響測試；改前先確認。）

- [ ] **Step 5: 跑測試＋全量**

Run: `python3 -m pytest tests/test_liquidation_cooldown.py -q && python3 -m pytest tests/ -q | tail -1`
Expected: 12 passed；全量 `129 passed`

- [ ] **Step 6: Commit**

```bash
git add src/orders.py src/sync.py tests/test_liquidation_cooldown.py
git commit -m "feat(orders,sync): liquidated coins enter cooldown — reduce-only until it expires"
```

---

### Task 5: 關鍵告警重試 `@inline`

**Files:**
- Modify: `src/telegram.py:25-51`（`_send`）、`alert_drawdown`、`alert_bot_stopped`、`alert_liquidated`
- Test: `tests/test_alert_retry.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_alert_retry.py
"""關鍵一次性告警（回撤停機、機器人停止、被清算）在 Telegram 網路失敗時重試。
為何：7 天 log 有 23 次 Telegram 網路失敗；_send 原本失敗只記 warning 不重試，
熔斷那則訊息若剛好碰到就丟了，使用者不會知道程式已停。"""
import src.telegram as telegram

REAL_SEND = telegram._send     # 在 conftest 的 autouse mute 之前取得真函式（模組載入時）


def _arm(monkeypatch):
    monkeypatch.setattr(telegram, "_BOT_TOKEN", "t")
    monkeypatch.setattr(telegram, "_CHAT_ID", "c")
    monkeypatch.setattr(telegram, "_recent_sent", {})
    monkeypatch.setattr(telegram._time, "sleep", lambda s: None)


class _Resp:
    def __init__(self, ok):
        self.ok = ok; self.status_code = 200 if ok else 500; self.text = "x"


def test_send_retries_until_success(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}
    def post(url, json=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("reset")
        return _Resp(True)
    monkeypatch.setattr(telegram.requests, "post", post)
    assert REAL_SEND("hi", retries=2) is True
    assert calls["n"] == 3


def test_send_gives_up_after_retries(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: calls.__setitem__("n", calls["n"] + 1) or _Resp(False))
    assert REAL_SEND("hi", retries=2) is False
    assert calls["n"] == 3


def test_send_default_no_retry(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}
    def post(*a, **k):
        calls["n"] += 1
        raise ConnectionError("reset")
    monkeypatch.setattr(telegram.requests, "post", post)
    assert REAL_SEND("hi") is False
    assert calls["n"] == 1


def test_dedup_checked_once_not_per_attempt(monkeypatch):
    _arm(monkeypatch)
    calls = {"n": 0}
    def post(*a, **k):
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("reset")
        return _Resp(True)
    monkeypatch.setattr(telegram.requests, "post", post)
    assert REAL_SEND("hi", dedup_key="k", retries=2) is True     # 第 2 次嘗試成功
    assert REAL_SEND("hi", dedup_key="k", retries=2) is False    # 5 分鐘內同 key → 去重


def test_critical_alerts_request_retries(monkeypatch):
    seen = []
    monkeypatch.setattr(telegram, "_send", lambda text, **kw: seen.append(kw.get("retries", 0)) or True)
    telegram.alert_drawdown(800.0, 1000.0, 0.2)
    telegram.alert_bot_stopped("回撤超過上限")
    telegram.alert_liquidated("xyz:CL", -9.5, "09/13 08:00")
    assert seen == [2, 2, 2]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `python3 -m pytest tests/test_alert_retry.py -q`
Expected: FAIL（`_send` 不認 `retries`）

- [ ] **Step 3: 改 `_send`**

把 `src/telegram.py:25-51` 換成：
```python
_RETRY_SLEEP = 2   # 重試間隔秒


def _send(text: str, dedup_key: str = None, retries: int = 0) -> bool:
    """送出訊息。成功回 True，否則 False（未設定/被去重/失敗）。
    retries：失敗後再試幾次（預設 0＝不重試）。只給一次性的關鍵告警用
    （回撤停機、機器人停止、被清算）；一般通知失敗就算了，避免拖慢主流程。
    去重只在第一次嘗試前判斷一次。"""
    if not _BOT_TOKEN or not _CHAT_ID:
        logger.debug("Telegram 未設定，跳過通知")
        return False
    if dedup_key is not None:
        now = _time.time()
        # 順手清掉過期項，避免無限長
        for k in [k for k, t in _recent_sent.items() if now - t > _DEDUP_TTL]:
            _recent_sent.pop(k, None)
        if now - _recent_sent.get(dedup_key, 0) < _DEDUP_TTL:
            return False
        _recent_sent[dedup_key] = now
    url = _API.format(token=_BOT_TOKEN)
    for attempt in range(retries + 1):
        try:
            resp = requests.post(
                url,
                json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"},
                timeout=8,
            )
            if resp.ok:
                return True
            logger.warning(f"Telegram 傳送失敗: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Telegram 例外: {e}")
        if attempt < retries:
            _time.sleep(_RETRY_SLEEP)
    return False
```

- [ ] **Step 4: 三個關鍵告警加 `retries=2`**

`alert_drawdown` 的 `_send(` 呼叫，在最後一個 f-string 參數後加 `, retries=2`；`alert_bot_stopped` 同樣；`alert_liquidated` 的 `dedup_key=...` 之後加 `retries=2`。

- [ ] **Step 5: 跑測試＋全量**

Run: `python3 -m pytest tests/test_alert_retry.py -q && python3 -m pytest tests/ -q | tail -1`
Expected: 5 passed；全量 `134 passed`

- [ ] **Step 6: Commit**

```bash
git add src/telegram.py tests/test_alert_retry.py
git commit -m "fix(telegram): retry critical one-shot alerts (drawdown stop, bot stopped, liquidated)"
```

---

### Task 6: `.env.example` 與 README 說明 `@sdd`

**Files:**
- Modify: `.env.example`（`ORDER_LEVERAGE` 段之後）
- Modify: `README.md`（「策略簡介」段落最後一句之後）

- [ ] **Step 1: `.env.example` 追加**

在 `ORDER_LEVERAGE=max` 那一行之後加：
```
# isolated 標的（xyz 美股、onlyIsolated 資產）的名目槓桿。isolated 的保證金＝名目/槓桿，
# 槓桿直接決定清算價，不能設 max。目標在該標的有部位時一律跟目標的槓桿；
# 目標沒有部位（純掛單）時用此值。預設 4。
ISOLATED_ORDER_LEVERAGE=4

# 清算冷卻（小時）：我方某標的被交易所清算後，此期間內該標的只准減倉/平倉，
# 不新開、不加倉、不掛補倉單，避免安全網在急拉後立刻市價重開又被清算。0=停用。預設 2。
LIQUIDATION_COOLDOWN_HOURS=2
```

- [ ] **Step 2: README 追加一句**

在 README「策略簡介」段落最後一句「名目槓桿一律設標的最大值（cross；xyz/onlyIsolated 自動改 isolated），只為節省保證金、不影響倉位大小。」之後改成：
```
名目槓桿：cross 標的設標的最大值（只為節省保證金、不影響倉位大小）；isolated 標的（xyz／onlyIsolated）跟目標的槓桿、無目標部位時用 `ISOLATED_ORDER_LEVERAGE`（預設 4x），因為 isolated 的槓桿直接決定清算價。被交易所清算的標的進入 `LIQUIDATION_COOLDOWN_HOURS`（預設 2 小時）冷卻，期間只准減倉。
```

- [ ] **Step 3: 驗收＋Commit**

Run: `grep -c "ISOLATED_ORDER_LEVERAGE\|LIQUIDATION_COOLDOWN_HOURS" .env.example README.md`
Expected: `.env.example:2`、`README.md:2`

```bash
git add .env.example README.md
git commit -m "docs: document ISOLATED_ORDER_LEVERAGE and LIQUIDATION_COOLDOWN_HOURS"
```

---

## 整體驗收（主線程親跑）

1. `python3 -m pytest tests/ -q | tail -1` → `134 passed`。
2. `python3 -m pyflakes src/ main.py` 或 `python3 -m compileall -q src main.py` 無錯。
3. `python3 main.py --once --dry-run 2>&1 | grep -E "lev=|清算冷卻|\[保護\]" | head` → xyz 標的槓桿等於目標槓桿或 4x；dry-run 無地址所以不出現清算冷卻（預期）。
4. `git log --oneline main..HEAD` 應有 6 個 commit。
5. reviewer 審 `git diff main...HEAD`。

## 部署與上線後觀察（使用者操作，本 plan 不執行）

- 部署指令 `sudo bash deploy/update.sh` 會重啟實盤 service，**由使用者自己執行**。
- **既有的 MINIMAX 部位仍是 10x isolated**：程式只在新開倉／掛單時設槓桿，不會主動改既有部位。使用者可在 HL UI 手動把該部位槓桿降到 3x（或等它平掉後自然以 3x 重進）。
- **待實測的未知**：isolated 已有部位時再 `updateLeverage` 成不同倍率，交易所是否接受。若被拒，`set_leverage` 會 log error＋發 `alert_error`（dedup 300 秒），開倉照常進行。上線後 24 小時內若看到「槓桿設定失敗」告警反覆出現，回報主線程，修法是：isolated 標的在我方已有部位時跳過 `set_leverage`。
- 觀察窗口：上線後第一個美股交易日，確認 log 出現 `lev=3x`／`lev=4x` 的 xyz 開倉，且無「槓桿設定失敗」。

## 不在範圍內（刻意）

- 10-8 安全網觸發改為「目標 size 變動」。
- 幻影部位調整（差額 < $10 仍記 action → TG 每分鐘一則）。
- 現貨單、小單門檻。

---

## Task 7: 審查修正（reviewer round 1）`@inline`

**審查結論與主線程複驗：**
- C1（fills 的 xyz 幣名可能無前綴）：主線程用真實資料複驗，30 天 351 筆 xyz 成交與 20 筆清算全部帶 `xyz:` 前綴，**不改碼**，只在 `liquidation.py` docstring 記一行「已對真實 payload 驗證：coin 帶 dex 前綴」。
- W5（真實 payload 未對帳）：主線程已用真錢包唯讀跑 `get_liquidation_cooldowns`（窗拉到 30 天），正確抓到 10 個標的／19 個事件，**已驗證**。
- 以下 C2、C3、W1、W2、W3、W4 要修。

**Files:**
- Modify: `src/liquidation.py`、`src/trader.py`（`entry_leverage`、`open_position`）、`src/sync.py`（`sync_positions`）、`src/orders.py`（`_build_desired`、`_set_entry_leverage`）、`src/telegram.py`（`alert_liquidated`）
- Test: `tests/test_liquidation_cooldown.py`、`tests/test_isolated_leverage.py`、新建 `tests/test_review_fixes.py`

- [ ] **Step 1: 先寫失敗測試（全部）**

`tests/test_liquidation_cooldown.py` 的 `test_sync_open_orders_blocks_cooling_coin_orders`：把 `fake_reconcile` 改成記 `[(d["coin"], d["reduce_only"]) for d in desired]`，斷言改成
```python
    assert seen["coins"] == [("xyz:CL", True), ("BTC", False)]   # C3：留下的 CL 必須是 reduce-only
```
並刪掉那行無意義的 `assert dry_trader._sz_dec`。

同檔 `test_new_liquidation_alerts_once` 的 monkeypatch 改成回 True（告警成功才記為已發）：
```python
    monkeypatch.setattr(telegram, "alert_liquidated", lambda coin, pnl, until: sent.append((coin, pnl)) or True)
```

新建 `tests/test_review_fixes.py`：
```python
"""Reviewer round-1 修正的回歸測試。每個測試對應一個 finding。"""
import pytest

from src import liquidation as liq, sync, orders, telegram
from src import trader as trader_mod
from src.trader import Trader

ME = "0x1A1d5eF3256e1A7de2db2082D7A1eEb976c90111"
NOW = 1_800_000_000.0


class FakeInfo:
    def meta(self, dex=""):
        if dex == "xyz":
            return {"universe": [{"name": "xyz:CL", "szDecimals": 3, "maxLeverage": 20}]}
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


class LevExchange:
    def __init__(self, lev_result):
        self.lev_result = lev_result; self.opened = 0
    def update_leverage(self, leverage, coin, is_cross): return self.lev_result
    def market_open(self, *a, **k):
        self.opened += 1
        return {"status": "ok", "response": {"data": {"statuses": [{}]}}}


# ── C2：冷快取 + 抓取失敗 → 重試、仍失敗要大聲叫 ─────────────────
def test_cold_cache_fetch_failure_retries_then_alerts(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}})
    monkeypatch.setattr(liq, "_alerted", set())
    monkeypatch.setattr(liq._time, "sleep", lambda s: None)
    calls = {"n": 0}
    def boom(api_url, payload):
        calls["n"] += 1
        raise RuntimeError("reset")
    monkeypatch.setattr(liq, "_post", boom)
    alerts = []
    monkeypatch.setattr(telegram, "alert_error", lambda t, d, extra="": alerts.append(t))
    assert liq.get_liquidation_cooldowns("api", ME, now=NOW) == {}
    assert calls["n"] == 3                       # 3 次嘗試
    assert alerts == ["清算冷卻表建立失敗"]       # 冷快取失敗 → 告警（不是靜默 {}）


def test_cold_cache_fetch_succeeds_on_retry(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}})
    monkeypatch.setattr(liq, "_alerted", set())
    monkeypatch.setattr(liq._time, "sleep", lambda s: None)
    calls = {"n": 0}
    def flaky(api_url, payload):
        calls["n"] += 1
        if calls["n"] < 2:
            raise RuntimeError("reset")
        return [{"coin": "xyz:CL", "time": int((NOW - 100) * 1000), "closedPnl": "-1",
                 "liquidation": {"liquidatedUser": ME.lower()}}]
    monkeypatch.setattr(liq, "_post", flaky)
    monkeypatch.setattr(telegram, "alert_liquidated", lambda *a: True)
    assert set(liq.get_liquidation_cooldowns("api", ME, now=NOW)) == {"xyz:CL"}


# ── W3：告警送失敗不記為已發，下次再送 ───────────────────────────
def test_failed_alert_is_retried_next_cycle(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}})
    monkeypatch.setattr(liq, "_alerted", set())
    fills = [{"coin": "xyz:CL", "time": int((NOW - 100) * 1000), "closedPnl": "-1",
              "liquidation": {"liquidatedUser": ME.lower()}}]
    monkeypatch.setattr(liq, "_post", lambda a, p: fills)
    results = iter([False, True])
    sent = []
    monkeypatch.setattr(telegram, "alert_liquidated", lambda *a: sent.append(a) or next(results))
    liq.get_liquidation_cooldowns("api", ME, now=NOW)
    liq._cache["ts"] = 0.0
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 120)
    liq._cache["ts"] = 0.0
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 240)
    assert len(sent) == 2                        # 第 1 次失敗 → 第 2 次再送 → 成功後不再送


def test_alert_liquidated_returns_send_result(monkeypatch):
    monkeypatch.setattr(telegram, "_send", lambda *a, **k: True)
    assert telegram.alert_liquidated("xyz:CL", -1.0, "09/13 08:00") is True


# ── W1：isolated 且我方已持有部位 → 沿用持有部位的槓桿，不跟目標改 ──
def test_isolated_held_position_keeps_its_leverage(monkeypatch):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    t = Trader(None, FakeInfo(), live_trading=False)
    assert t.entry_leverage("xyz:CL", target_leverage=10, held_leverage=3) == 3
    assert t.entry_leverage("xyz:CL", target_leverage=10) == 10
    assert t.entry_leverage("BTC", target_leverage=5, held_leverage=3) == 40   # cross 不受影響


def test_safety_net_adjust_passes_held_leverage(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    monkeypatch.setattr(sync, "SIZE_TOLERANCE", 0.05)
    dry_trader._sz_dec["xyz:CL"] = 3
    seen = {}
    monkeypatch.setattr(dry_trader, "adjust_position",
                        lambda coin, cur, tgt, cs, ts, leverage, is_cross, **kw: seen.update(leverage=leverage))
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "short", "size": 1000.0, "entry_px": 100.0,
           "leverage": 10, "leverage_type": "isolated", "notional": 100000.0, "unrealized_pnl": 0.0}
    mine = {"xyz:CL": {"side": "short", "size": 1.0, "leverage": 3, "unrealized_pnl": 0.0}}
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                        {"account_value": 1000.0, "positions": mine}, scale=0.002)   # 目標 2.0 vs 我 1.0 → 加倉
    assert seen["leverage"] == 3


def test_build_desired_carries_held_leverage(dry_trader):
    dry_trader._sz_dec["xyz:CL"] = 3
    o = {"coin": "xyz:CL", "is_buy": False, "size": 1000.0, "limit_px": 100.0, "trigger_px": 0.0,
         "reduce_only": False, "is_trigger": False, "tpsl": None, "is_market": False, "tif": "Gtc",
         "order_type_name": "Limit"}
    desired, *_ = orders._build_desired(dry_trader, [o], scale=0.01, protected=set(),
                                        my_positions={"xyz:CL": {"side": "short", "size": 1.0, "leverage": 3}},
                                        target_positions={"xyz:CL": {"leverage": 10}})
    assert desired[0]["target_leverage"] == 10 and desired[0]["held_leverage"] == 3


def test_set_entry_leverage_prefers_held(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    calls = []
    monkeypatch.setattr(dry_trader, "set_leverage", lambda c, l, x: calls.append(l) or True)
    orders._set_entry_leverage(dry_trader, {"coin": "xyz:CL", "reduce_only": False, "target_leverage": 10, "held_leverage": 3})
    assert calls == [3]


# ── W2：isolated 槓桿設定失敗 → 不開倉 ───────────────────────────
def test_isolated_leverage_failure_skips_open(monkeypatch):
    ex = LevExchange({"status": "err", "response": "Cannot change leverage with open position"})
    t = Trader(ex, FakeInfo(), live_trading=True)
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    res = t.open_position("xyz:CL", False, 1.0, 4, False, entry_px=100.0)
    assert res is None and ex.opened == 0


def test_cross_leverage_failure_still_opens(monkeypatch):
    ex = LevExchange({"status": "err", "response": "boom"})
    t = Trader(ex, FakeInfo(), live_trading=True)
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "notify_open", lambda *a, **k: None)
    monkeypatch.setattr(trader_mod, "_position_exists", lambda *a, **k: True)
    t.open_position("BTC", True, 0.001, 40, True, entry_px=100.0)
    assert ex.opened == 1                         # cross：槓桿只影響保證金，照舊行為


# ── W4：冷卻中不准反向翻倉 ────────────────────────────────────────
def test_protected_coin_blocks_reversal(monkeypatch, dry_trader):
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    called = []
    monkeypatch.setattr(dry_trader, "adjust_position", lambda *a, **k: called.append(a))
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "long", "size": 1000.0, "entry_px": 100.0,
           "leverage": 4, "leverage_type": "isolated", "notional": 100000.0, "unrealized_pnl": 0.0}
    mine = {"xyz:CL": {"side": "short", "size": 1.0, "leverage": 4, "unrealized_pnl": 0.0}}
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                        {"account_value": 1000.0, "positions": mine}, protected={"xyz:CL"}, scale=0.002)
    assert called == []
```

- [ ] **Step 2: 跑確認失敗**

Run: `python3 -m pytest tests/test_review_fixes.py tests/test_liquidation_cooldown.py -q`
Expected: `test_review_fixes.py` 至少 8 個 FAIL；`test_liquidation_cooldown.py` 的 C3 測試 FAIL（現在 `seen["coins"]` 是字串清單）。

- [ ] **Step 3: 實作**

**`src/liquidation.py`**
1. docstring 加一行：`- 已對真實 payload 驗證（2026-09-13，30 天 351 筆 xyz 成交、20 筆清算）：fills 的 coin 一律帶 dex 前綴（xyz:CL），與 monitor._canon_coin 一致。`
2. `_my_liquidations` 加重試（3 次、0.5s/1s 退避）：
```python
_FETCH_ATTEMPTS = 3


def _my_liquidations(api_url: str, address: str, since_ms: int) -> list:
    """回傳 [(coin, time_ms, closed_pnl)]，只含「我方是被清算方」的成交。
    唯讀查詢屬冪等，transient 失敗最多重試 _FETCH_ATTEMPTS 次。"""
    last_err = None
    for i in range(_FETCH_ATTEMPTS):
        try:
            fills = _post(api_url, {"type": "userFillsByTime", "user": address, "startTime": since_ms})
            break
        except Exception as e:
            last_err = e
            if i < _FETCH_ATTEMPTS - 1:
                _time.sleep(0.5 * (2 ** i))
    else:
        raise last_err
    me = address.lower()
    out = []
    for f in fills:
        liq = f.get("liquidation")
        if not liq or str(liq.get("liquidatedUser", "")).lower() != me:
            continue
        out.append((f["coin"], int(f["time"]), float(f.get("closedPnl", 0) or 0)))
    return out
```
3. `get_liquidation_cooldowns` 的 except 分支改成「冷快取要大聲叫」：
```python
    except Exception as e:
        cached = {c: u for c, u in _cache["until"].items() if u > now}
        if _cache["address"] == address and _cache["ts"] > 0:
            logger.warning(f"取得清算紀錄失敗，沿用上次冷卻表({len(cached)} 個標的): {e}")
        else:
            logger.error(f"取得清算紀錄失敗且無快取，本輪無清算冷卻保護: {e}")
            tg.alert_error("清算冷卻表建立失敗", f"{e}", "本輪無清算冷卻保護，下輪重試")
        return cached
```
4. 告警成功才記為已發（W3），並裁剪 `_alerted`：
```python
        key = (coin, t_ms)
        if key not in _alerted:
            until_str = datetime.fromtimestamp(end).strftime("%m/%d %H:%M")
            logger.warning(f"[清算冷卻] {coin} 於 {datetime.fromtimestamp(t_ms/1000):%m/%d %H:%M} 被清算"
                           f"（已實現 {pnl:+.2f}），冷卻至 {until_str}，期間只准減倉")
            if tg.alert_liquidated(coin, pnl, until_str):
                _alerted.add(key)
    # 裁剪已過窗的告警紀錄，避免行程壽命內無限增長
    cutoff_ms = int((now - window_s) * 1000)
    _alerted.difference_update({k for k in _alerted if k[1] < cutoff_ms})
```

**`src/telegram.py`** `alert_liquidated` 改回傳 `bool`：`def alert_liquidated(coin, pnl, until_str) -> bool: return _send(...)`。

**`src/trader.py`**
1. `entry_leverage(self, coin, target_leverage=0, held_leverage=0)`：isolated 分支改成
```python
        if not self.entry_is_cross(coin):
            # 已持有 isolated 部位 → 沿用它的槓桿（改倍率會直接動到既有部位的清算價）；
            # 否則跟目標；目標無部位用預設。
            if held_leverage and held_leverage > 0:
                want = int(held_leverage)
            elif target_leverage and target_leverage > 0:
                want = int(target_leverage)
            else:
                want = ISOLATED_ORDER_LEVERAGE
            return max(1, min(want, max_lev) if max_lev > 0 else want)
```
docstring 補一句「held_leverage：我方已持有該標的部位的槓桿，isolated 時優先沿用」。
2. `open_position` 第 163 行 `self.set_leverage(coin, leverage, is_cross)` 改成：
```python
        if not self.set_leverage(coin, leverage, is_cross) and not is_cross:
            # isolated：槓桿決定清算價，設定失敗就不能用未知倍率開倉（cross 只影響保證金，照常）
            logger.error(f"[SKIP] {coin} isolated 槓桿 {leverage}x 設定失敗，跳過開倉")
            tg.alert_error("isolated 槓桿設定失敗，跳過開倉", f"{coin} {leverage}x")
            return None
```

**`src/sync.py`** `sync_positions`：
1. 第 1 段迴圈裡 `leverage = trader.entry_leverage(coin, tgt_pos.get("leverage", 0))` 改成
```python
        held = my_positions.get(coin, {}).get("leverage", 0)
        leverage = trader.entry_leverage(coin, tgt_pos.get("leverage", 0), held)
```
2. 在 `if coin in protected and target_side == my_side and target_size > my_size:` 之前加：
```python
            if coin in protected and target_side != my_side:
                logger.warning(f"[保護] {coin} 抗單/清算冷卻中，跳過反向翻倉（{my_side}→{target_side}）")
                continue
```

**`src/orders.py`**
1. `_build_desired` 的 spec 加 `"held_leverage": (my_positions.get(coin) or {}).get("leverage", 0),`。
2. `_set_entry_leverage` 改成 `trader.entry_leverage(coin, desired.get("target_leverage", 0), desired.get("held_leverage", 0))`。

- [ ] **Step 4: 跑全量**

Run: `python3 -m pytest tests/ -q | tail -1`
Expected: `145 passed`（134 + 11）

- [ ] **Step 5: Commit**

```bash
git add src/ tests/
git commit -m "fix: review round-1 — cooldown fail-loud on cold cache, held isolated leverage wins, skip open on isolated lev failure, block reversal under protection, alert retry semantics"
```

---

## Task 8: 審查修正（reviewer round 2）`@inline`

**主線程裁決：** C-1、C-2、W-1、W-3、W-4 修；W-2 改為「連續 3 輪抓取失敗就 alert_error（dedup）」，**不**阻擋新開倉（阻擋是語意變更，留給使用者決定）；Suggestion 3（conftest mute 回 True）採納；Suggestion 1、2、4 不做。

**Files:**
- Modify: `src/instrument.py`（新 helper）、`src/trader.py`（`entry_leverage`、新 `prepare_entry`、`open_position`）、`src/orders.py`（`_build_desired`、`_set_entry_leverage` 與 3 個呼叫端）、`src/sync.py`（held 來源、反向守門改為平倉）、`src/liquidation.py`（連續失敗計數）、`src/telegram.py`（`_send` dedup 只在成功時佔用）、`tests/conftest.py`（mute 回 True）
- Test: `tests/test_review_fixes.py`（追加）、`tests/test_review_fixes_r2.py`（新建）

- [ ] **Step 1: 寫失敗測試**

`tests/test_review_fixes.py` 的 `test_isolated_held_position_keeps_its_leverage` 改成（W-1：held 只當上限，可以往下）：
```python
def test_isolated_held_position_caps_leverage(monkeypatch):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    t = Trader(None, FakeInfo(), live_trading=False)
    assert t.entry_leverage("xyz:CL", target_leverage=10, held_leverage=3) == 3    # 不可高於持有
    assert t.entry_leverage("xyz:CL", target_leverage=4, held_leverage=20) == 4    # 可以往下（加保證金、清算價推遠）
    assert t.entry_leverage("xyz:CL", held_leverage=20) == 4                       # 舊 20x 部位 → 之後補倉用 4x
    assert t.entry_leverage("xyz:CL", target_leverage=10) == 10
    assert t.entry_leverage("BTC", target_leverage=5, held_leverage=3) == 40       # cross 不受影響
```

新建 `tests/test_review_fixes_r2.py`：
```python
"""Reviewer round-2 修正的回歸測試。"""
import src.telegram as telegram
from src import liquidation as liq, sync, orders, instrument
from src import trader as trader_mod
from src.trader import Trader

REAL_SEND = telegram._send
ME = "0x1A1d5eF3256e1A7de2db2082D7A1eEb976c90111"
NOW = 1_800_000_000.0


class FakeInfo:
    def meta(self, dex=""):
        if dex == "xyz":
            return {"universe": [{"name": "xyz:CL", "szDecimals": 3, "maxLeverage": 20}]}
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}


class LevExchange:
    def __init__(self, lev_result):
        self.lev_result = lev_result; self.orders = 0
    def update_leverage(self, leverage, coin, is_cross): return self.lev_result
    def order(self, *a, **k):
        self.orders += 1
        return {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 1}}]}}}


def _spec(coin, reduce_only=False):
    return {"coin": coin, "is_buy": False, "size": 1.0, "limit_px": 100.0, "trigger_px": 0.0,
            "reduce_only": reduce_only, "is_trigger": False, "tpsl": None, "is_market": False,
            "tif": "Gtc", "order_type_name": "Limit", "target_leverage": 0, "held_leverage": 0}


# ── C-1：保護中目標翻向 → 平掉我方部位，但不開反向 ─────────────────
def test_protected_reversal_closes_but_does_not_reopen(monkeypatch, dry_trader):
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    closed, opened, adjusted = [], [], []
    monkeypatch.setattr(dry_trader, "close_position", lambda *a, **k: closed.append(a) or {"status": "dry_run"})
    monkeypatch.setattr(dry_trader, "open_position", lambda *a, **k: opened.append(a))
    monkeypatch.setattr(dry_trader, "adjust_position", lambda *a, **k: adjusted.append(a))
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "long", "size": 1000.0, "entry_px": 100.0,
           "leverage": 4, "leverage_type": "isolated", "notional": 100000.0, "unrealized_pnl": 0.0}
    mine = {"xyz:CL": {"side": "short", "size": 1.0, "leverage": 4, "leverage_type": "isolated", "unrealized_pnl": -2.0}}
    res = sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                              {"account_value": 1000.0, "positions": mine}, protected={"xyz:CL"}, scale=0.002)
    assert len(closed) == 1 and closed[0][0] == "xyz:CL" and closed[0][2] == 1.0
    assert opened == [] and adjusted == []
    assert [a["action"] for a in res["actions"]] == ["close"]


# ── C-2：掛單路徑 isolated 槓桿設定失敗 → 不掛 ───────────────────
def test_set_entry_leverage_returns_false_on_isolated_failure(monkeypatch):
    ex = LevExchange({"status": "err", "response": "boom"})
    t = Trader(ex, FakeInfo(), live_trading=True)
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    assert orders._set_entry_leverage(t, _spec("xyz:CL")) is False
    assert orders._set_entry_leverage(t, _spec("BTC")) is True          # cross 失敗仍放行
    assert orders._set_entry_leverage(t, _spec("xyz:CL", reduce_only=True)) is True


def test_reconcile_skips_isolated_order_when_leverage_fails(monkeypatch):
    ex = LevExchange({"status": "err", "response": "boom"})
    t = Trader(ex, FakeInfo(), live_trading=True)
    t._sz_dec = {"xyz:CL": 3, "BTC": 5}
    monkeypatch.setattr(telegram, "alert_error", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "notify_order_placed", lambda *a, **k: None)
    monkeypatch.setattr(telegram, "alert_order_sync_failed", lambda *a, **k: None)
    # 驗證重抓：交易所上只有 BTC 那張（CL 被閘門擋掉），CL 會被判「缺少」→ 補缺再被擋 → sync_failed
    mine_btc = {"coin": "BTC", "is_buy": False, "reduce_only": False, "is_trigger": False,
                "limit_px": 100.0, "trigger_px": 0.0, "size": 1.0, "oid": 1, "tpsl": None, "is_market": False}
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [mine_btc])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    monkeypatch.setattr(trader_mod, "_order_rests", lambda *a, **k: True)
    res = orders._reconcile_orders(t, "api", ME, [_spec("xyz:CL"), _spec("BTC")], [])
    assert ex.orders == 1            # 只有 BTC 掛出去；CL 在首掛與補缺兩處都被擋
    assert res["placed"] == 1
    assert res["sync_failed"] is True   # 持續被擋會每輪報同步失敗，屬「大聲失敗」的預期行為


# ── W-3：_send 失敗不佔用 dedup，下一輪可立即補送 ───────────────────
class _Resp:
    def __init__(self, ok): self.ok = ok; self.status_code = 500; self.text = "x"


def test_send_failure_does_not_consume_dedup(monkeypatch):
    monkeypatch.setattr(telegram, "_BOT_TOKEN", "t"); monkeypatch.setattr(telegram, "_CHAT_ID", "c")
    monkeypatch.setattr(telegram, "_recent_sent", {}); monkeypatch.setattr(telegram._time, "sleep", lambda s: None)
    results = iter([_Resp(False), _Resp(True), _Resp(True)])
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: next(results))
    assert REAL_SEND("x", dedup_key="k") is False
    assert REAL_SEND("x", dedup_key="k") is True       # 失敗沒佔用 → 立即補送成功
    assert REAL_SEND("x", dedup_key="k") is False      # 成功後才進入 5 分鐘去重


# ── W-4：held 只在部位本身是 isolated 時才採用 ───────────────────────
def test_held_isolated_leverage_helper():
    assert instrument.held_isolated_leverage({"leverage": 3, "leverage_type": "isolated"}) == 3
    assert instrument.held_isolated_leverage({"leverage": 40, "leverage_type": "cross"}) == 0
    assert instrument.held_isolated_leverage({}) == 0
    assert instrument.held_isolated_leverage(None) == 0


def test_build_desired_ignores_cross_held_leverage(dry_trader):
    dry_trader._sz_dec["xyz:CL"] = 3
    desired, *_ = orders._build_desired(dry_trader, [_spec("xyz:CL")], scale=1.0, protected=set(),
                                        my_positions={"xyz:CL": {"side": "short", "size": 1.0, "leverage": 40, "leverage_type": "cross"}},
                                        target_positions={})
    assert desired[0]["held_leverage"] == 0


def test_safety_net_ignores_cross_held_leverage(monkeypatch, dry_trader):
    monkeypatch.setattr(trader_mod, "ISOLATED_ORDER_LEVERAGE", 4)
    monkeypatch.setattr(sync, "get_mid_price", lambda api, coin: 100.0)
    dry_trader._sz_dec["xyz:CL"] = 3
    seen = {}
    monkeypatch.setattr(dry_trader, "adjust_position",
                        lambda coin, cur, tgt, cs, ts, leverage, is_cross, **kw: seen.update(leverage=leverage))
    tgt = {"coin": "xyz:CL", "dex": "xyz", "side": "short", "size": 1000.0, "entry_px": 100.0,
           "leverage": 10, "leverage_type": "isolated", "notional": 100000.0, "unrealized_pnl": 0.0}
    mine = {"xyz:CL": {"side": "short", "size": 1.0, "leverage": 40, "leverage_type": "cross", "unrealized_pnl": 0.0}}
    sync.sync_positions("api", dry_trader, {"account_value": 1000.0, "failed_dexs": set(), "positions": {"xyz:CL": tgt}},
                        {"account_value": 1000.0, "positions": mine}, scale=0.002)
    assert seen["leverage"] == 10                       # 不採用 cross 的 40


# ── W-2：熱快取連續失敗要告警 ────────────────────────────────────────
def test_hot_cache_repeated_failures_alert(monkeypatch):
    monkeypatch.setattr(liq, "COOLDOWN_HOURS", 2.0)
    monkeypatch.setattr(liq, "_cache", {"ts": 0.0, "address": None, "until": {}, "failures": 0})
    monkeypatch.setattr(liq, "_alerted", set())
    monkeypatch.setattr(liq._time, "sleep", lambda s: None)
    monkeypatch.setattr(telegram, "alert_liquidated", lambda *a: True)
    fills = [{"coin": "xyz:CL", "time": int((NOW - 100) * 1000), "closedPnl": "-1",
              "liquidation": {"liquidatedUser": ME.lower()}}]
    monkeypatch.setattr(liq, "_post", lambda a, p: fills)
    liq.get_liquidation_cooldowns("api", ME, now=NOW)                 # 熱快取建立
    alerts = []
    monkeypatch.setattr(telegram, "alert_error", lambda t, d, extra="": alerts.append(t))
    def boom(a, p): raise RuntimeError("429")
    monkeypatch.setattr(liq, "_post", boom)
    for i in range(1, 4):
        liq._cache["ts"] = 0.0
        liq.get_liquidation_cooldowns("api", ME, now=NOW + 60 * i)
    assert alerts == ["清算冷卻表連續抓取失敗"]                      # 第 3 次才叫，之後靠 dedup
    liq._cache["ts"] = 0.0
    monkeypatch.setattr(liq, "_post", lambda a, p: fills)
    liq.get_liquidation_cooldowns("api", ME, now=NOW + 300)
    assert liq._cache["failures"] == 0                                 # 成功歸零
```

- [ ] **Step 2: 跑確認失敗**

Run: `python3 -m pytest tests/test_review_fixes_r2.py tests/test_review_fixes.py -q`
Expected: r2 至少 7 個 FAIL（`held_isolated_leverage` 不存在、`_set_entry_leverage` 回 None、翻向不平倉、dedup 佔用、`failures` 鍵不存在）；`test_isolated_held_position_caps_leverage` FAIL（held=20 現在回 20）。

- [ ] **Step 3: 實作**

**`src/instrument.py`** 末尾加：
```python
def held_isolated_leverage(pos) -> int:
    """我方已持有部位的槓桿，只在該部位本身是 isolated 時才回傳（cross 的倍率是帳戶設定，
    與 isolated 清算價無關，不能拿來當 held 上限）。無部位／cross → 0。"""
    if not pos or pos.get("leverage_type") != "isolated":
        return 0
    try:
        return int(pos.get("leverage", 0) or 0)
    except (TypeError, ValueError):
        return 0
```

**`src/trader.py`**
1. import 加 `held_isolated_leverage`（不必，helper 由呼叫端用）；`entry_leverage` 的 isolated 分支改成「held 是上限，不是優先」：
```python
        if not self.entry_is_cross(coin):
            # 想要的倍率：跟目標，目標無部位用預設；若我方已持有 isolated 部位，
            # 不得高於它（調高會釋放既有部位保證金、拉近清算價），可以往下（加保證金）。
            want = int(target_leverage) if target_leverage and target_leverage > 0 else ISOLATED_ORDER_LEVERAGE
            if held_leverage and held_leverage > 0:
                want = min(want, int(held_leverage))
            return max(1, min(want, max_lev) if max_lev > 0 else want)
```
2. 新增單一閘門（部位與掛單共用），放在 `set_leverage` 之後：
```python
    def prepare_entry(self, coin: str, leverage: int, is_cross: bool, what: str = "進場") -> bool:
        """進場前設定槓桿的唯一閘門。cross：設定失敗只影響保證金，放行；
        isolated：槓桿決定清算價，設定失敗就不准進場（回 False，呼叫端必須跳過）。"""
        ok = self.set_leverage(coin, leverage, is_cross)
        if ok or is_cross:
            return True
        logger.error(f"[SKIP] {coin} isolated 槓桿 {leverage}x 設定失敗，跳過{what}")
        tg.alert_error(f"isolated 槓桿設定失敗，跳過{what}", f"{coin} {leverage}x")
        return False
```
3. `open_position` 裡 Task 7 加的那段 `if not self.set_leverage(...) and not is_cross: ... return None` 換成：
```python
        if not self.prepare_entry(coin, leverage, is_cross, "開倉"):
            return None
```

**`src/orders.py`**
1. import 加 `held_isolated_leverage`（從 `.instrument`）。
2. `_build_desired` 的 `"held_leverage": (my_positions.get(coin) or {}).get("leverage", 0),` 改成 `"held_leverage": held_isolated_leverage(my_positions.get(coin)),`。
3. `_set_entry_leverage` 改回傳 bool 並走閘門：
```python
def _set_entry_leverage(trader: Trader, desired: dict) -> bool:
    """進場單（非 reduce-only）下單前設定名目槓桿。回 False 表示 isolated 槓桿設定失敗、
    這張單不得掛出（呼叫端必須跳過）。cross 用 max；isolated 跟目標／持有上限／預設。"""
    if desired["reduce_only"]:
        return True
    coin = desired["coin"]
    return trader.prepare_entry(
        coin,
        trader.entry_leverage(coin, desired.get("target_leverage", 0), desired.get("held_leverage", 0)),
        trader.entry_is_cross(coin),
        "掛單",
    )
```
4. 三個呼叫端（modify 段、to_place 段、驗證補缺段）都改成：
```python
        if not _set_entry_leverage(trader, d):
            continue
```
（modify 段的變數名是 `spec`，照原變數名。）

**`src/sync.py`**
1. import 加 `held_isolated_leverage`；`held = my_positions.get(coin, {}).get("leverage", 0)` 改成 `held = held_isolated_leverage(my_positions.get(coin))`。
2. Task 7 加的反向守門改成「平掉、不反向」：
```python
            # 保護（抗單/清算冷卻）：目標翻向時只平掉我方部位，不開反向
            if coin in protected and target_side != my_side:
                logger.warning(f"[保護] {coin} 抗單/清算冷卻中，目標翻向（{my_side}→{target_side}）：只平倉、不反向")
                is_buy_close = my_side == "long"
                result = trader.close_position(
                    coin, is_buy_close, my_size,
                    unrealized_pnl=my_pos.get("unrealized_pnl", 0),
                    my_address=my_address, api_url=api_url,
                )
                actions.append({"action": "close", "coin": coin, "result": result})
                continue
```

**`src/liquidation.py`**
1. `_cache` 初始化改成 `_cache = {"ts": 0.0, "address": None, "until": {}, "failures": 0}`；新增 `_FAIL_ALERT_AFTER = 3`。
2. except 分支改成：
```python
    except Exception as e:
        cached = {c: u for c, u in _cache["until"].items() if u > now}
        _cache["failures"] = _cache.get("failures", 0) + 1
        if _cache["address"] == address and _cache["ts"] > 0:
            logger.warning(f"取得清算紀錄失敗（連續 {_cache['failures']} 次），沿用上次冷卻表({len(cached)} 個標的): {e}")
            if _cache["failures"] >= _FAIL_ALERT_AFTER:
                tg.alert_error("清算冷卻表連續抓取失敗", f"連續 {_cache['failures']} 次: {e}",
                               "冷卻表可能過期，新清算偵測不到")
        else:
            logger.error(f"取得清算紀錄失敗且無快取，本輪無清算冷卻保護: {e}")
            tg.alert_error("清算冷卻表建立失敗", f"{e}", "本輪無清算冷卻保護，下輪重試")
        return cached
```
3. 成功路徑 `_cache.update(ts=now, address=address, until=until)` 改成 `_cache.update(ts=now, address=address, until=until, failures=0)`。

**`src/telegram.py`** `_send`：把 `_recent_sent[dedup_key] = now` 從嘗試前移到成功時：
```python
    if dedup_key is not None:
        now = _time.time()
        for k in [k for k, t in _recent_sent.items() if now - t > _DEDUP_TTL]:
            _recent_sent.pop(k, None)
        if now - _recent_sent.get(dedup_key, 0) < _DEDUP_TTL:
            return False
    url = _API.format(token=_BOT_TOKEN)
    for attempt in range(retries + 1):
        try:
            resp = requests.post(url, json={"chat_id": _CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=8)
            if resp.ok:
                if dedup_key is not None:
                    _recent_sent[dedup_key] = _time.time()   # 成功才佔用去重視窗；失敗下次可立即補送
                return True
            logger.warning(f"Telegram 傳送失敗: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"Telegram 例外: {e}")
        if attempt < retries:
            _time.sleep(_RETRY_SLEEP)
    return False
```
既有 `tests/test_alert_retry.py::test_dedup_checked_once_not_per_attempt` 的語意仍成立（第 2 次嘗試成功後才佔用；之後同 key 去重）。

**`tests/conftest.py`** `_mute_telegram`：`lambda *a, **k: None` 改成 `lambda *a, **k: True`（`_send` 的布林現在是控制流，mute 應代表「送成功」）。

- [ ] **Step 4: 跑全量**

Run: `python3 -m pytest tests/ -q | tail -1`
Expected: `154 passed`（145 + 9）。`test_review_fixes.py::test_isolated_leverage_failure_skips_open` 仍應通過（改走 `prepare_entry`）。

- [ ] **Step 5: Commit**

```bash
git add src/ tests/
git commit -m "fix: review round-2 — single entry-leverage gate for orders+positions, held isolated leverage is a cap, protected reversal closes without reopening, dedup only on successful send, alert on repeated cooldown fetch failure"
```
