# 連續改單根治：scale 遲滯 + 波動權重改用已完成日

日期：2026-08-20
狀態：**已實作、全套測試綠（101 passed），待 commit 與部署**（選項 A 已確認，2026-08-20）

## 根因（已驗證，不要重新推導）

改單風暴（實測 343 次/小時、每次耗 1 個位址額度、零成交量）的根因是 **`scale` 抖動**：

- `scale = ALLOCATED_CAPITAL × CAPITAL_UTILIZATION × weight / 目標淨值`（`sync.py compute_scale_factor`）。
  生產設定本金=1000、使用率=1.0 皆固定；目標淨值每小時只漂 ~0.2%；**變動幾乎全來自 `weight`**
  （由日誌反推：一小時內 0.956→0.81，漂 15%）。
- `weight = 1 − clip(z×0.2, 0, 0.7)`，`z = (today − μ)/σ`（`weight.py`）。**bug：`today` 是目標帳戶
  「進行中的部分日」|PnL|，而 μ/σ 用「完整日」計算**——基準錯配（工程原則第 1 條），
  z 隨時間經過機械性爬升，weight 整個交易日單調下降。`_CACHE_TTL=300` 使其每 5 分鐘重算。
- weight 一動 → 全部 desired size 同時變 → 一起跨過 `SIZE_TOLERANCE=5%` →
  `_orders_match` 全部不匹配 → **整本 81 筆改單**。實測敏感度：σ=1,811 時
  today 變動 $226（$735k 帳戶的 0.03%）即觸發整本重寫。
- 證據：scale 穩定的連續 9 個 cycle 改單=0；爆量 cycle 與 scale 變動一一對應，
  間隔皆 ≥5 分鐘（對上快取 TTL）。
- 同一根因也造成部位側的 sub-$10 調整騷擾（KIOXIA 目標 size 漂移），目前被 $10 門檻擋下。
- **注意第二個類比通道**：`compute_scale_factor` 內 `MAX_TARGET_LEVERAGE=10`（生產已啟用）
  的壓縮項 `scale *= MAX/eff_lev` 也是連續變數，目前休眠（eff_lev 0.29x），但目標瀕臨爆倉時
  會甦醒並重演同樣的整本重寫。**所以護欄必須裝在 scale 層，不是 weight 層。**

## 設計決策（已裁決）

- **Task 1（主修）**：scale 層遲滯——新算出的 scale 與「套用中的 scale」相對差異
  < `SCALE_HYSTERESIS`（預設 0.10）就沿用舊值。覆蓋所有現在與未來的抖動源。
- **Task 2（次修，待確認選項 A）**：z 只用已完成的日。妖度保護語義從「當天」變「隔天生效」；
  日內爆倉場景由既有 `MAX_TARGET_LEVERAGE` 護欄承擔。兩項合計預期效果：
  整本重寫從 ~343 次/小時 → 最多 1 次/天（UTC 換日時）。
- **不做**：拆分 SIZE_TOLERANCE（治標）；不動 `_prices_equal`（價格容忍度從未造成問題）。

---

## Task 1 @inline：scale 遲滯（單一計算點 + 套用值日誌）

### 要求

1. `src/config.py` 新增 `SCALE_HYSTERESIS = _env_float("SCALE_HYSTERESIS", "0.10")`，
   合法範圍 `0 ≤ x < 1`，超界回預設。`0` = 停用（=現行為）。`.env.example` 加註解說明。
2. `src/sync.py` 新增模組級狀態與函式（命名可調，語義不可調）：
   ```python
   _applied_scale: Optional[float] = None

   def get_stable_scale(trader_equity, my_equity, target_notional=0.0) -> float:
       """算 raw scale 後套用遲滯：與套用中值相對差異 < SCALE_HYSTERESIS 就沿用舊值。"""
   ```
   採用規則（顯式方程式）：`adopt ⟺ _applied_scale is None or
   abs(raw − _applied_scale) >= SCALE_HYSTERESIS × _applied_scale`。
   **數值錨例**（機器可驗證）：套用中 0.00130、新算 0.00124（差 4.6%）→ 沿用 0.00130；
   套用中 0.00130、新算 0.00116（差 10.8%）→ 採用 0.00116；恰好 10.0% → 採用（用 `>=`）。
3. **單一計算點**：`orders.py sync_open_orders` 改呼叫 `get_stable_scale`（取代
   `compute_scale_factor`），並把結果經新增的可選參數傳入 `sync_positions(..., scale=...)`；
   `sync_positions` 收到 `scale is not None` 就直接用、不重算（`None` 時維持現行內部計算，
   供既有測試與相容性）。**一個 cycle 內兩處必須用同一個值。**
4. **日誌（診斷能力，使用者明確要求）**：遲滯生效（沿用舊值）的 cycle，在既有的
   「跟單比例」INFO 行加註原始值，例如 `比例 0.0013（新算 0.00124，差 4.6% < 10%，沿用）`；
   未生效時維持現行格式。措辭風格與既有中文日誌一致。
5. `compute_scale_factor` 本身**不動**（純函式，繼續被 `get_stable_scale` 呼叫）。
6. 已知邊界（保持現行為、不要順手改）：raw=0（目標淨值讀取失敗等）與套用中值差異必為
   100% ≥ 帶寬 → 照常採用 0。這是既有語義（scale=0 → 全部撤單），本 plan 不改變它，
   只在 plan 留此觀察記錄。

### 驗收條件

1. `python3 -m pytest tests/ -q` 全綠。動手前先跑基線（**90 passed**），改完再跑，兩次輸出都貼。
2. 新增測試：依上方三個數值錨例逐一斷言 `get_stable_scale` 的採用/沿用行為。
3. 新增測試：首次呼叫（無套用中值）→ 直接採用 raw。
4. 新增測試：`SCALE_HYSTERESIS=0` → 每次都採用 raw（=現行為）。
5. 新增測試：`sync_positions` 收到 `scale` 參數時不重算（mock `compute_scale_factor`
   斷言未被呼叫）；未收到時行為與現行一致。
6. **測試有效性證明**：暫時把採用規則改回「一律採用 raw」，確認錨例測試 FAIL，再改回。
   把「移除 → FAIL」輸出貼在回報裡。不能跳過。

---

## Task 2 @inline：波動權重的 z 只用已完成的日（選項 A，待使用者確認）

### 要求

1. `src/weight.py` `compute_volatility_stats`：**排除進行中的今日**後再算。
   判別規則（顯式）：`_daily_abs_pnl` 回傳序列的**最後一個日桶的 UTC 日期 == 當前 UTC 日期**
   → 該桶是部分日，丟棄；**否則**（今日尚無樣本，最後桶已是完整日）→ 不丟。
   丟棄後：`current = 完整日序列[-1]`，`baseline = 其前最多 LOOKBACK_DAYS 天`，
   `z = (current − μ(baseline)) / σ(baseline)`。
   - 實作提示：`_daily_abs_pnl` 目前只回 |PnL| list、不帶日期，需要讓日期資訊可及
     （改回傳帶日期的結構、或在函式內部完成丟棄——實作者擇一，測試要覆蓋判別規則）。
2. **數值錨例**：日桶（UTC 日期, 累積PnL）=
   `[(D-16,0), (D-15,100), …完整日…, (D-1,cum_a), (D0=今天, cum_b)]`：
   今天的桶被丟棄，`cum_b` 完全不影響 z；把系統時鐘 mock 到 D0+1（今天沒有新桶）時，
   D0 桶變成完整日、參與計算。
3. 資料不足規則維持既有精神：丟棄部分日後不足 3 個完整日 → 回 `None`（weight 回 1，安全預設）。
4. 快取 TTL=300 **不動**（日內恆定改由演算法保證，不靠快取）。
5. **日誌**：目標波動權重的值與上次計算不同時，INFO 一行列出 z/current/μ/σ/weight
   （現在 `get_position_weight` 全程沉默，這次診斷得靠 SSH 反推 scale 才找到它——
   這行日誌就是為了下次不用）。預期日內至多出現一次。
6. docstring 更新：明寫「妖度訊號為日級、隔日生效；日內爆倉防護由 MAX_TARGET_LEVERAGE 承擔」。

### 驗收條件

1. `python3 -m pytest tests/ -q` 全綠（含 Task 1 新增的測試）。
2. 新增測試：依錨例——部分日桶存在時被排除（改變 cum_b 不改變 z）；
   今日無桶時最後完整日不被誤丟（防 off-by-one）。
3. 新增測試：同一 UTC 日內兩次計算（不同的部分日 |PnL|）→ weight 相同（日內恆定）。
4. 新增測試：丟棄後不足 3 個完整日 → 回 None。
5. **測試有效性證明**：暫時還原「不丟部分日」的舊行為，確認測試 2、3 FAIL，再改回。輸出貼在回報裡。

---

## 全域約束

- 改動限於 `src/sync.py`、`src/orders.py`、`src/weight.py`、`src/config.py`（僅新增設定項）、
  `.env.example`（僅新增註解與預設值）與 `tests/`。
  **不要動** `src/trader.py`、`src/telegram.py`、`src/monitor.py`、`main.py`、`.env`。
- 禁改：`_prices_equal`、`SIZE_TOLERANCE`、`_reconcile_orders`、`MIN_ORDER_NOTIONAL` 的既有檢查、
  `compute_scale_factor` 的公式本體、`MAX_TARGET_LEVERAGE` 邏輯。
- 先讀 `src/sync.py`、`src/weight.py`、`src/orders.py` 既有寫法，中文日誌措辭、命名、
  型別註記風格保持一致。
- 測試不得打真實網路／真實 API（`~/.claude/rules/engineering-principles.md` 第 4 條）；
  時間相關測試用 monkeypatch，不得 sleep、不得依賴真實時鐘日期。
- 這是**實盤**程式碼。任何失敗路徑不得靜默吞掉（第 3 條）。

## 與其他 plan 的關係

- `2026-08-20-mid-price-failure-alert.md`（5.1 取價告警）：獨立、互不依賴，可各自開發。
- 部署順序建議：兩個 plan 都完成並審查後一次部署重啟（重啟會觸發一次初始同步，
  scale 遲滯狀態歸零屬預期行為——首輪直接採用 raw）。

---

## 開工前裁決（主線程，2026-08-20，使用者已確認選項 A）

1. **Task 1 日誌改為「採用時 INFO、沿用時 DEBUG」**，取代原「在既有比例行加註」。
   理由：沿用是常態（每分鐘一次），INFO 會變成無資訊量的洗版；「比例真的變了」
   才是診斷需要的事件（預期每天個位數次）。既有的比例 INFO 行照常印套用值，
   沿用狀態仍可從該行讀出。
2. **Task 2 的部分日剔除改為在 `_daily_abs_pnl` 內部完成**，回傳型別維持 `list[float]` 不變。
   理由：`tests/test_vol_partial.py` 有 5 處 monkeypatch 此函式的舊契約（純 float list）；
   在函式內剔除可讓既有測試零改動（它們語義上變成「皆為完整日」，仍正確），
   diff 更小、更機械。`compute_volatility_stats` 本體不動，只更新 docstring
   （dict key `today` 保留原名＝最近完整日，因 `main.py`/`telegram.py` 讀此 key 且屬禁改檔）。
3. **邊界錨例改用二進位可精確表示的數**：套用中 1.0、新算 0.875、帶寬 0.125（恰好 12.5%）
   → 採用。原 0.00130/0.00117 的 10.0% 邊界在浮點下不可靠（0.1 非二進位精確），
   會做出 flaky 測試。規則本體不變（`>=`）。
4. **已知限制（記錄，不修）**：`main.py:191` 顯示「今日|PnL|」的標籤在 Task 2 之後
   語義變為「最近完整日」，`main.py` 屬禁改檔，標籤暫不動；weight.py 新增的
   權重更新日誌使用正確標籤「最近完整日|PnL|」。
5. **測試隔離**：`get_stable_scale` 引入模組級狀態，conftest 需新增 autouse fixture
   重置 `sync._applied_scale`，否則跨測試殘留會使既有 `sync_open_orders` 測試
   受前一個測試的 scale 污染。
