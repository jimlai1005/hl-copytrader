# Gap Analysis Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正 hl-copytrader 的資料一致性漏洞（最關鍵：單一 DEX 查詢失敗會誤把我方部位/掛單砍光），並建立第一批自動化測試。

**Architecture:** 在 `get_trader_state` / `get_trader_open_orders` 回傳「查詢失敗的 DEX 集合」，讓部位安全網與掛單對帳對「查詢失敗的 DEX」一律跳過平倉/撤單（資料不可信時寧可不動）。其餘修正都圍繞同一原則：缺資料時不做破壞性動作。同時導入 pytest，所有修正走 TDD。

**Tech Stack:** Python 3.9、pytest、monkeypatch（mock 掉 `monitor._post` 網路層，測試全離線）。

**Phase 範圍：** 本計畫只做 Phase 1（測試基建 + 正確性修正 G1–G4）。Phase 2（架構重構）在文末列為後續獨立計畫，**不要**在本計畫執行。

---

## File Structure

| 檔案 | 角色 / 變更 |
|------|------------|
| `requirements-dev.txt` | 新增：pytest 等開發相依 |
| `tests/__init__.py` | 新增：空檔，讓 tests 成為 package |
| `tests/conftest.py` | 新增：共用 fixture（dry-run Trader、假 _post） |
| `tests/test_dex_failure.py` | 新增：G1 測試（DEX 查詢失敗保護） |
| `tests/test_reduce_only_guard.py` | 新增：G4 測試 |
| `tests/test_modify_skip.py` | 新增：G3 測試 |
| `src/monitor.py:73-118,167-215` | 修改：`get_trader_state` / `get_trader_open_orders` 回傳 `failed_dexs` |
| `src/sync.py` | 修改：安全網對 failed_dexs 跳過平倉；import `_coin_dex` |
| `src/orders.py` | 修改：對帳排除 failed_dexs 我方掛單；G4 reduce-only 守門；G3 modify 跳過快取 |
| `main.py:142-175` | 修改：`run_sync` 合併兩個 failed_dexs 後往下傳；接住 `get_trader_open_orders` 新回傳 |

**設計原則：** `failed_dexs` 是 `set[str]`（如 `{"xyz"}`）。判斷某 coin 是否屬失敗 DEX 用既有的 `trader._coin_dex(coin)`（`"xyz:NVDA"→"xyz"`、`"BTC"→""`）。

---

## Task 1: 測試基建（pytest）

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`
- Create: `tests/test_smoke.py`

- [ ] **Step 1: 建立 dev 相依與 tests package**

`requirements-dev.txt`:
```
-r requirements.txt
pytest>=8.0
```

`tests/__init__.py`: （空檔）

- [ ] **Step 2: 寫共用 fixture**

`tests/conftest.py`:
```python
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.trader import Trader


@pytest.fixture(autouse=True)
def _offline_weight(monkeypatch):
    """測試預設停用波動權重，避免 compute_scale_factor 走 portfolio 網路 API。"""
    from src import weight
    monkeypatch.setattr(weight, "get_vol_stats", lambda address: None)


@pytest.fixture
def dry_trader():
    """乾跑 Trader：不連線、不下單；下單/平倉只回 dry_run。"""
    t = Trader(None, None, live_trading=False)
    # 預填 size decimals，避免在無 info 時走 meta 查詢
    t._sz_dec = {"BTC": 5, "ETH": 4, "xyz:NVDA": 2, "SOL": 2}
    return t


def make_pos(coin, side="long", size=1.0, notional=100.0, leverage=10,
             lev_type="cross", upnl=0.0, entry_px=100.0):
    return {
        "coin": coin, "side": side, "size": size, "notional": notional,
        "leverage": leverage, "leverage_type": lev_type,
        "unrealized_pnl": upnl, "entry_px": entry_px,
    }
```

- [ ] **Step 3: 寫煙霧測試確認可跑**

`tests/test_smoke.py`:
```python
def test_imports():
    import main  # noqa: F401
    from src import config, monitor, orders, trader, sync, telegram, weight, protection  # noqa: F401


def test_coin_dex():
    from src.trader import _coin_dex
    assert _coin_dex("xyz:NVDA") == "xyz"
    assert _coin_dex("BTC") == ""
```

- [ ] **Step 4: 安裝並執行**

Run: `pip install -r requirements-dev.txt && python -m pytest tests/ -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add requirements-dev.txt tests/__init__.py tests/conftest.py tests/test_smoke.py
git commit -m "test: add pytest infra and smoke tests"
```

---

## Task 2: G1a — `get_trader_state` 回傳 failed_dexs

**Files:**
- Modify: `src/monitor.py`（`get_trader_state`，約 73-118 行）
- Test: `tests/test_dex_failure.py`

- [ ] **Step 1: 寫失敗測試**

`tests/test_dex_failure.py`:
```python
import pytest
from src import monitor


def _fake_post_xyz_fails(api_url, payload):
    if payload.get("dex") == "xyz":
        raise RuntimeError("network blip")
    if payload["type"] == "clearinghouseState":
        return {"marginSummary": {"accountValue": "1000", "totalRawUsd": "1000"},
                "assetPositions": []}
    return {}


def test_get_trader_state_marks_failed_dex(monkeypatch):
    monkeypatch.setattr(monitor, "EXTRA_DEXS", ["xyz"])
    monkeypatch.setattr(monitor, "_post", _fake_post_xyz_fails)
    state = monitor.get_trader_state("api", "0xabc")
    assert state["failed_dexs"] == {"xyz"}
    assert state["account_value"] == 1000.0


def test_get_trader_state_no_failure(monkeypatch):
    def ok_post(api_url, payload):
        return {"marginSummary": {"accountValue": "500", "totalRawUsd": "500"},
                "assetPositions": []}
    monkeypatch.setattr(monitor, "EXTRA_DEXS", ["xyz"])
    monkeypatch.setattr(monitor, "_post", ok_post)
    state = monitor.get_trader_state("api", "0xabc")
    assert state["failed_dexs"] == set()
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_dex_failure.py -q`
Expected: FAIL（`KeyError: 'failed_dexs'`）

- [ ] **Step 3: 實作**

在 `src/monitor.py` 的 `get_trader_state` 內，把 EXTRA_DEXS 迴圈改成收集失敗，並把 `failed_dexs` 放進回傳。完整替換該函式的迴圈與 return：
```python
    # 2. 額外 DEX（xyz 美股）：accountValue 加總；查詢失敗記錄該 DEX
    failed_dexs = set()
    for dex in EXTRA_DEXS:
        try:
            dex_data = _post(api_url, {
                "type": "clearinghouseState",
                "user": address,
                "dex": dex,
            })
            account_value += float(dex_data["marginSummary"].get("accountValue") or 0)
            positions.update(_parse_positions(dex_data, dex=dex))
        except Exception as e:
            logger.warning(f"查詢 {dex} DEX 倉位失敗（將跳過該 DEX 的平倉判斷）: {e}")
            failed_dexs.add(dex)

    if include_spot:
        account_value += _spot_usdc(api_url, address)

    return {
        "account_value": account_value,
        "positions": positions,
        "failed_dexs": failed_dexs,
    }
```

- [ ] **Step 4: 執行確認通過**

Run: `python -m pytest tests/test_dex_failure.py -q`
Expected: PASS（2 passed）

- [ ] **Step 5: Commit**

```bash
git add src/monitor.py tests/test_dex_failure.py
git commit -m "fix: get_trader_state reports failed dexs (G1a)"
```

---

## Task 3: G1b — 安全網對 failed_dexs 跳過平倉

**Files:**
- Modify: `src/sync.py`（import `_coin_dex`；section 2 close 迴圈）
- Test: `tests/test_dex_failure.py`（新增）

- [ ] **Step 1: 寫失敗測試（接續同檔）**

在 `tests/test_dex_failure.py` 末尾新增：
```python
from src import sync
from tests.conftest import make_pos


def test_safety_net_skips_failed_dex_close(dry_trader):
    target_state = {"account_value": 1000, "positions": {}, "failed_dexs": {"xyz"}}
    my_state = {"account_value": 1000, "positions": {
        "xyz:NVDA": make_pos("xyz:NVDA", lev_type="isolated"),
        "BTC": make_pos("BTC", size=0.01, notional=600, entry_px=60000),
    }}
    result = sync.sync_positions("api", dry_trader, target_state, my_state)
    closed = [a["coin"] for a in result["actions"] if a["action"] == "close"]
    assert "BTC" in closed            # 正常 DEX：目標已平 → 跟著平
    assert "xyz:NVDA" not in closed   # xyz 查詢失敗 → 不可平
```

注意：`dry_trader` 與 `make_pos` 來自 `tests/conftest.py`（make_pos 需在 conftest 匯出，Task 1 已定義為模組層函式，可 `from tests.conftest import make_pos`）。

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_dex_failure.py::test_safety_net_skips_failed_dex_close -q`
Expected: FAIL（`xyz:NVDA` 被平倉，assert 失敗）

- [ ] **Step 3: 實作**

在 `src/sync.py` 頂部 import 加入 `_coin_dex`：
```python
from .trader import Trader, _coin_dex
```
在 `sync_positions` 開頭（`protected = protected or set()` 之後）取出 failed_dexs：
```python
    failed_dexs = target_state.get("failed_dexs", set())
```
在 section 2「我有但目標已平的標的 → 跟著平」迴圈最前面加守門（找到該迴圈，於 `if coin not in target_positions:` 之後、執行平倉前插入）：
```python
        if coin not in target_positions:
            if _coin_dex(coin) in failed_dexs:
                logger.warning(f"[資料保護] {coin} 所屬 DEX 查詢失敗，本輪跳過平倉")
                continue
            # （以下維持原本的平倉邏輯不變）
```

- [ ] **Step 4: 執行確認通過**

Run: `python -m pytest tests/test_dex_failure.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/sync.py tests/test_dex_failure.py
git commit -m "fix: safety net skips closing positions on failed dex (G1b)"
```

---

## Task 4: G1c — 掛單查詢回傳 failed_dexs，對帳排除失敗 DEX 我方掛單

**Files:**
- Modify: `src/monitor.py`（`get_trader_open_orders` 回傳 `(orders, failed_dexs)`）
- Modify: `src/orders.py`（`sync_open_orders` 排除 failed_dexs 我方掛單）
- Modify: `main.py`（接住新回傳、合併 failed_dexs）
- Test: `tests/test_dex_failure.py`

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_dex_failure.py` 末尾新增：
```python
from src import orders


def test_get_trader_open_orders_returns_failed(monkeypatch):
    def post(api_url, payload):
        if payload.get("dex") == "xyz":
            raise RuntimeError("blip")
        return []
    monkeypatch.setattr(monitor, "EXTRA_DEXS", ["xyz"])
    monkeypatch.setattr(monitor, "_post", post)
    result_orders, failed = monitor.get_trader_open_orders("api", "0xabc")
    assert result_orders == []
    assert failed == {"xyz"}


def test_reconcile_keeps_my_orders_on_failed_dex(dry_trader):
    # 目標掛單空（因 xyz 失敗），我有一張 xyz 掛單 → 不可被當 extra 取消
    my_orders = [{"coin": "xyz:NVDA", "oid": 1, "is_buy": True, "limit_px": 100,
                  "trigger_px": 0, "size": 1.0, "reduce_only": False,
                  "is_trigger": False, "tpsl": None, "is_market": False, "tif": "Gtc",
                  "order_type_name": "Limit"}]
    target_state = {"account_value": 1000, "positions": {}, "failed_dexs": {"xyz"}}
    my_state = {"account_value": 1000, "positions": {}}
    res = orders.sync_open_orders("api", dry_trader, target_state, my_state,
                                  target_orders=[], my_orders=my_orders,
                                  skip_safety_net=True)
    assert res["cancelled"] == 0   # xyz 掛單被保護，不取消
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_dex_failure.py -q`
Expected: FAIL（`get_trader_open_orders` 還回 list 不是 tuple；reconcile 取消了 xyz 單）

- [ ] **Step 3a: 實作 monitor.get_trader_open_orders**

把 `src/monitor.py` 的 `get_trader_open_orders` 回傳改為 `(orders, failed_dexs)`：
```python
def get_trader_open_orders(api_url: str, address: str) -> tuple:
    """取得交易員所有未成交掛單。回傳 (orders, failed_dexs)。"""
    orders = []
    failed_dexs = set()
    try:
        orders.extend(_parse_orders(_post(api_url, {"type": "frontendOpenOrders", "user": address})))
    except Exception as e:
        logger.warning(f"查詢預設 DEX 掛單失敗: {e}")
        failed_dexs.add("")
    for dex in EXTRA_DEXS:
        try:
            dex_data = _post(api_url, {"type": "frontendOpenOrders", "user": address, "dex": dex})
            orders.extend(_parse_orders(dex_data, dex=dex))
        except Exception as e:
            logger.warning(f"查詢 {dex} DEX 掛單失敗（將跳過該 DEX 撤單）: {e}")
            failed_dexs.add(dex)
    return orders, failed_dexs
```
同步更新 `get_my_open_orders`（它呼叫 `get_trader_open_orders`，只要回 orders）：
```python
def get_my_open_orders(api_url: str, address: str) -> list:
    orders, _failed = get_trader_open_orders(api_url, address)
    return orders
```

- [ ] **Step 3b: 實作 orders.sync_open_orders 排除**

在 `src/orders.py` import 區加：
```python
from .trader import Trader, _round_size, _is_spot_coin, _coin_dex
```
在 `sync_open_orders` 內、`rec = _reconcile_orders(...)` 之前，過濾我方掛單：
```python
    failed_dexs = target_state.get("failed_dexs", set())
    my_orders = [m for m in my_orders if _coin_dex(m["coin"]) not in failed_dexs]
```
（放在 `_build_desired` 之後、`_reconcile_orders` 之前。被過濾掉的我方掛單完全不進對帳 → 不會被當 extra 取消。）

- [ ] **Step 3c: 實作 main.run_sync 接線**

在 `main.py` `run_sync` 的 section 1：
```python
    target_orders, order_failed_dexs = get_trader_open_orders(HL_API_URL, TARGET_TRADER)
```
緊接著把兩個失敗集合併入 target_state（讓掛單對帳與安全網共用）：
```python
    target_state["failed_dexs"] = target_state.get("failed_dexs", set()) | order_failed_dexs
```
同時更新 `--status` 分支的呼叫（`main()` 內）：
```python
        orders, _failed = get_trader_open_orders(HL_API_URL, TARGET_TRADER)
```

- [ ] **Step 4: 執行確認通過**

Run: `python -m pytest tests/ -q`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add src/monitor.py src/orders.py main.py tests/test_dex_failure.py
git commit -m "fix: skip cancelling my orders on failed dex query (G1c)"
```

---

## Task 5: G4 — reduce-only 掛單需有對應部位才鏡像

**Files:**
- Modify: `src/orders.py`（`_build_desired` 加 `my_positions` 守門）
- Test: `tests/test_reduce_only_guard.py`

- [ ] **Step 1: 寫失敗測試**

`tests/test_reduce_only_guard.py`:
```python
from src import orders


def _order(coin, reduce_only):
    return {"coin": coin, "is_buy": False, "limit_px": 200, "trigger_px": 0,
            "size": 1.0, "reduce_only": reduce_only, "is_trigger": False,
            "tpsl": None, "is_market": False, "tif": "Gtc", "order_type_name": "Limit"}


def test_reduce_only_skipped_without_position(dry_trader):
    # 我沒有 ETH 部位 → ETH 的 reduce-only 單不該進 desired
    target_orders = [_order("ETH", reduce_only=True), _order("BTC", reduce_only=False)]
    desired, _small, _spot, _prot = orders._build_desired(
        dry_trader, target_orders, scale=1.0, protected=set(), my_positions={})
    coins = {d["coin"] for d in desired}
    assert "ETH" not in coins   # reduce-only 無部位 → 跳過
    assert "BTC" in coins       # 一般進場單 → 保留


def test_reduce_only_kept_with_position(dry_trader):
    from tests.conftest import make_pos
    target_orders = [_order("ETH", reduce_only=True)]
    desired, *_ = orders._build_desired(
        dry_trader, target_orders, scale=1.0, protected=set(),
        my_positions={"ETH": make_pos("ETH")})
    assert {d["coin"] for d in desired} == {"ETH"}
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_reduce_only_guard.py -q`
Expected: FAIL（`_build_desired` 還沒有 `my_positions` 參數 → TypeError）

- [ ] **Step 3: 實作**

`src/orders.py` 的 `_build_desired` 簽章加 `my_positions`：
```python
def _build_desired(trader: Trader, target_orders: list, scale: float,
                   protected: set = None, my_positions: dict = None) -> tuple:
```
函式開頭：
```python
    protected = protected or set()
    my_positions = my_positions or {}
```
在迴圈內、`if coin in protected and not o["reduce_only"]:` 守門之後，加 reduce-only 守門：
```python
        if o["reduce_only"] and coin not in my_positions:
            continue   # G4: 沒有對應部位的 reduce-only 單會被交易所拒，直接跳過
```
更新 `sync_open_orders` 呼叫處，傳入 `my_positions=my_state.get("positions", {})`：
```python
    desired, skipped_small, skipped_spot, skipped_protected = _build_desired(
        trader, target_orders, scale, set(protected), my_state.get("positions", {})
    )
```

- [ ] **Step 4: 執行確認通過**

Run: `python -m pytest tests/test_reduce_only_guard.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orders.py tests/test_reduce_only_guard.py
git commit -m "fix: skip mirroring reduce-only orders without a held position (G4)"
```

---

## Task 6: G3 — modify 連續失敗的標的本輪直接 cancel+place

**Files:**
- Modify: `src/orders.py`（`_reconcile_orders` 加 modify 跳過快取）
- Test: `tests/test_modify_skip.py`

> 取捨提醒：此優化只在「保證金長期吃緊、modify 註定失敗」時省一個 API call。若先前未遇到該情況可先略過本 Task。

- [ ] **Step 1: 寫失敗測試**

`tests/test_modify_skip.py`:
```python
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
    # 標記 BTC 剛失敗過 → 本輪不該再呼叫 modify_order
    orders._modify_fail_until["BTC"] = time.time() + orders._MODIFY_SKIP_TTL
    called = {"modify": 0}
    monkeypatch.setattr(dry_trader, "modify_order", lambda oid, spec: called.__setitem__("modify", called["modify"] + 1) or True)
    desired = [_order("BTC", px=99)]
    mine = [_order("BTC", oid=1, px=100)]
    res = orders._reconcile_orders(dry_trader, "api", "", desired, mine)
    assert called["modify"] == 0          # 跳過 modify
    assert res["cancelled"] == 1 and res["placed"] == 1   # 直接 cancel+place
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_modify_skip.py -q`
Expected: FAIL（`_modify_fail_until` / `_MODIFY_SKIP_TTL` 未定義）

- [ ] **Step 3: 實作**

`src/orders.py` 模組層常數區（`SIZE_TOLERANCE` 附近）加：
```python
# modify 失敗的標的，此秒數內直接走 cancel+place（避免註定失敗的 modify 浪費呼叫）
_MODIFY_SKIP_TTL = 120
_modify_fail_until = {}   # coin -> 解除跳過的 unix 秒
```
在 `_reconcile_orders` 的 modify 迴圈，改成：跳過近期失敗者、modify 失敗時登記：
```python
    import time as _t
    now = _t.time()
    modified = 0
    fallback = []
    for oid, spec in modifies:
        coin = spec["coin"]
        if now < _modify_fail_until.get(coin, 0):
            fallback.append((oid, coin, spec))      # 近期失敗過 → 直接退回 cancel+place
            continue
        _set_entry_leverage(trader, spec)
        if trader.modify_order(oid, spec):
            modified += 1
            _modify_fail_until.pop(coin, None)
        else:
            _modify_fail_until[coin] = now + _MODIFY_SKIP_TTL
            logger.info(f"改單 {coin} oid={oid} 失敗，退回先取消再重掛")
            fallback.append((oid, coin, spec))
```

- [ ] **Step 4: 執行確認通過**

Run: `python -m pytest tests/ -q`
Expected: PASS（全部）

- [ ] **Step 5: Commit**

```bash
git add src/orders.py tests/test_modify_skip.py
git commit -m "perf: skip modify for coins that recently failed, go straight to cancel+place (G3)"
```

---

## Task 7（可選，需先確認取捨）: G2 — 安全網執行前重抓我方部位

> **執行前先與使用者確認**：此修正讓安全網更即時（避免週期內成交→重複下單），但每輪多一個 API call、且讓「掛單對帳」與「安全網」用不同時間點的快照。若使用者偏好低 API 用量、接受下一輪自我修正，則**不做**此 Task。

**Files:**
- Modify: `src/orders.py`（`sync_open_orders` 在呼叫 `sync_positions` 前，live 模式重抓 my positions）

- [ ] **Step 1: 寫失敗測試**

`tests/test_resync_positions.py`:
```python
from src import orders, monitor


def test_safety_net_refetches_positions_live(monkeypatch, dry_trader):
    dry_trader.live_trading = True
    calls = {"n": 0}
    def fake_my_state(api, addr):
        calls["n"] += 1
        return {"account_value": 1000, "positions": {}}
    monkeypatch.setattr(orders, "get_my_state", fake_my_state, raising=False)
    # 隔離：sync_positions 不做事、驗證重抓掛單回空、不真的 sleep
    monkeypatch.setattr(orders, "sync_positions", lambda **k: {"actions": []})
    monkeypatch.setattr(orders, "get_my_open_orders", lambda api, addr: [])
    monkeypatch.setattr(orders.time, "sleep", lambda s: None)
    target_state = {"account_value": 1000, "positions": {}, "failed_dexs": set()}
    my_state = {"account_value": 1000, "positions": {}}
    orders.sync_open_orders("api", dry_trader, target_state, my_state,
                            target_orders=[], my_orders=[], my_address="0xabc")
    assert calls["n"] == 1   # 安全網前重抓一次
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_resync_positions.py -q`
Expected: FAIL（沒有 import get_my_state / 沒有重抓）

- [ ] **Step 3: 實作**

`src/orders.py` import 加 `get_my_state`：
```python
from .monitor import get_my_open_orders, get_my_state
```
在 `sync_open_orders` 的安全網段落，呼叫 `sync_positions` 之前（非 skip_safety_net、live、有 my_address 時）重抓：
```python
        if trader.live_trading and my_address:
            fresh = get_my_state(api_url, my_address)
            my_state = {**my_state, "positions": fresh["positions"]}
```

- [ ] **Step 4: 執行確認通過**

Run: `python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/orders.py tests/test_resync_positions.py
git commit -m "fix: refetch my positions before safety net to avoid stale-snapshot double-acting (G2)"
```

---

## Final Verification

- [ ] **全測試 + 編譯 + 端到端乾跑**

```bash
python -m pytest tests/ -q
python -m py_compile main.py src/*.py
python main.py --once --dry-run
```
Expected: 測試全 PASS；編譯 OK；乾跑無例外、log 正常。

- [ ] **更新記憶**：在專案記憶記下 G1–G4 修正與測試基建已完成。

---

## Phase 2（後續獨立計畫，本計畫不執行）— 架構重構

> 建議 Phase 1 上線並 live 驗證穩定後，另開計畫執行。每項都應在有測試保護下小步進行。

| 優先 | 項目 | 檔案 | 做法摘要 |
|------|------|------|---------|
| P0 | 拆 `trader.py` God class | `src/trader.py` → `executor.py` / `leverage.py` | 分 OrderExecutor / PositionExecutor / LeverageManager，Trader 變 facade |
| P1 | 收斂 `open/close_position`(75/66 行) | `src/trader.py` | 抽 `_market_close_xyz/_default`、`_handle_order_result` |
| P1 | 合併三個 meta 查詢 | `src/trader.py` | 一個 `_meta_field(info, coin, field, default)` |
| P1 | 收斂 SDK 回應耦合 | `src/monitor.py` / `src/trader.py` | 解析集中到 parser 層，其他模組只碰正規化 dict |
| P1 | `main.run_sync` 拆分 | `main.py` | `_fetch_states/_check_drawdown/_report_volatility`；排程抽 Scheduler |
| P2 | 策略魔法值入 config | `weight.py`/`protection.py`/`config.py` | Z 曲線(0.2/0.7/14)、MIN_TRADES 等參數化 |
| P2 | `_post` 提升為公開 api client | 新 `src/api.py` | 消除跨模組 import 私有函式 |
