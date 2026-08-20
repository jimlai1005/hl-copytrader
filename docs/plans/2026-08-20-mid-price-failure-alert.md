# 取價失敗時發出 Telegram 告警

日期：2026-08-20
狀態：**已實作（2026-08-20，主線程 inline），全套測試綠（105 passed）**

## 背景（已驗證，不要重新推導）

`get_mid_price`（`src/monitor.py:235`）取價失敗時只有 `logger.warning`，不進 Telegram。
而生產環境的日誌等級是 **INFO 寫死**（`main.py:45`，沒有 `LOG_LEVEL` 設定），
使用者不會主動去翻 log，等於**取價失敗完全不可見**。

取價失敗的下游後果（`src/sync.py:97`）：
`mid_px = get_mid_price(api_url, coin) or tgt_pos["entry_px"]`
→ 兩者都失敗時 `mid_px = 0` → `notional = 0 < MIN_ORDER_NOTIONAL` → `continue`
→ **該標的本輪被跳過**。使用者完全不知道有標的被跳過了。

### 已裁決：不加 in-cycle retry

主迴圈在美股活躍時段是**每分鐘**跑一次（`main.py:333-334`），非活躍時段依
`OFFHOURS_SYNC_MODE`（生產設定為 `5min`）。所以「跳過本輪、下一輪自然重來」**已經是**
內建的重試機制，不需要在 cycle 內再包一層 retry。

不加 in-cycle retry 的理由（使用者已確認）：
- 同步迴圈有時效性（正在鏡像一個活的掛單簿），阻塞式重試會拖慢後面所有動作。
- 取價失敗通常是暫時性網路問題，1 分鐘是合理的退避間隔。
- `src/resilience.py` 的重試機制是**刻意只包寫入**的；讀取失敗讓下一輪自然重來是既有設計。

**所以本 plan 只做「告警」，不動任何控制流程。**

---

## Task 1 @inline：`get_mid_price` 失敗時發 Telegram 告警

### 要求

1. 在 `src/telegram.py` 新增告警函式（比照既有 `alert_*` 函式的風格與簽名慣例）。
   內容要讓使用者一眼看懂：哪個標的、取價失敗、本輪已跳過、下一輪會自動重試。
2. 在取價失敗的路徑發出該告警。**注意呼叫點的選擇**：
   - `get_mid_price` 本身有兩個呼叫端（`src/sync.py:97`、`src/trader.py:316`），
     兩處的後果不同（前者跳過標的、後者跳過 xyz 平倉），告警訊息要能區分是哪一種。
   - 實作者自行判斷要在 `monitor.py` 內發、還是在兩個呼叫端各自發；
     判準是「訊息要能讓使用者知道實際後果」，而不是只說「取價失敗」。
3. **去重**：沿用既有 `_send(..., dedup_key=...)` 機制（`_DEDUP_TTL = 300` 秒），
   dedup key 帶 coin。理由：`get_mid_price` 是**每個標的每輪**呼叫，API 若整段掛掉
   會是數十個標的 × 每分鐘 → 會打爆 Telegram 的 rate limit 反而收不到訊息。
   5 分鐘一則仍然足夠吵、使用者不會漏看。
   - **這不是「靜音」**：這是本專案既有的告警慣例（`alert_rate_limited`、
     `alert_insufficient_balance` 都用同一套），不是新發明的抑制機制。

### 明確不要做的事

- **不要**加 in-cycle retry 迴圈（見上方裁決）。
- **不要**改 `sync.py:97` 那行的 `or tgt_pos["entry_px"]` 後備邏輯。
- **不要**加任何前置條件（例如 `if mid_px > 0`）或改變既有控制流程。
  這次只加告警，行為完全不變。
- **不要**碰 `_prices_equal`、`SIZE_TOLERANCE`、`_reconcile_orders`、
  `MIN_ORDER_NOTIONAL` 的任何既有檢查。

### 驗收條件

1. `python3 -m pytest tests/ -q` 全綠。**動手前先跑一次確認基線**（基線為 90 passed），
   改完再跑，兩次輸出都貼出來。
2. 新增測試：`get_mid_price` 回 None 時，對應的告警函式被呼叫一次（mock 斷言），
   且 `sync_positions` 的控制流程與現況完全一致（該標的被跳過，不多不少）。
3. 新增測試：同一 coin 連續多輪取價失敗，在 `_DEDUP_TTL` 內 Telegram 實際送出次數
   受既有 dedup 機制限制（驗證不會打爆）。
4. **測試有效性證明**：暫時移除告警呼叫，確認上述測試會 FAIL，再改回。
   把「移除 → FAIL」的輸出貼在回報裡。這條不能跳過。
5. 測試不打真實網路／真實 API；沒有新增未處理的 exception 路徑；
   沒有印出或硬編碼 secrets。

---

## 全域約束

- 改動限於 `src/monitor.py`、`src/sync.py`、`src/trader.py`、`src/telegram.py` 與 `tests/`。
  **不要動** `src/orders.py`、`src/config.py`、`src/weight.py`、`main.py`、`.env*`。
- 先讀 `src/telegram.py` 既有的 `alert_*` 函式，新函式的中文措辭、HTML 格式、
  `dedup_key` 用法要與它們一致。
- 專案紅線：先讀專案根目錄 `CLAUDE.md`（若存在）並遵守。
- 測試不得打真實網路／真實 API（`~/.claude/rules/engineering-principles.md` 第 4 條）。
- 這是**實盤**程式碼。任何失敗路徑不得靜默吞掉（第 3 條）。

---

## 不在本 plan 範圍（已分析完成，等使用者決定是否開工）

以下為 2026-08-19～20 調查的結論，**尚未排入開發**：

1. **連續改單的根因**：`weight` 每 5 分鐘重算（`weight.py:28` 的 `_CACHE_TTL=300`），
   而 `z = (today − μ)/σ` 的 `today` 是目標帳戶**當日累積**的 |PnL|，整個 session 都在長。
   weight 一動 → `scale` 一動 → 81 筆 desired size 同時變 → 全部超過
   `SIZE_TOLERANCE=5%` → 整本重寫。實測 σ=1,811，**today 只要變動 $226
   （$735k 帳戶的 0.03%）就會觸發整本 81 筆改單**。
   建議解法三層：(a) z 只用已完成的日計算〔修正部分日 vs 完整日的基準錯配，
   屬原則 1 的 bug〕；(b) weight 加遲滯，差異 <10% 不採用〔結構性護欄，最推薦〕；
   (c) `SIZE_TOLERANCE` 拆成掛單用／部位用兩個。
2. **`_round_size` 用 `math.floor` 會製造 dust**：全平時 0.0599（szDecimals=2）只送 0.05，
   殘留 0.0099；下一輪 round 成 0 → `trader.py:212` `if size <= 0: return None`
   **靜默 return，無日誌無告警，部位永久卡住**。
3. **`close_position` 沒有跑 `_extract_order_error`**：被拒的平倉單回 `status: ok`，
   程式當成成功並發 `notify_close` 附上 PnL。使用者已決定**不修**——
   重複出現的平倉通知本身就是他要的偵測訊號（唯一注意：該通知的 PnL 數字在拒單時不可信）。
4. **額度斜率告警**：位址額度是終身預算（`10,000 + 累積成交量`），只被「動作」消耗。
   目前無任何主動預警，`alert_rate_limited` 是落後指標（訂單被拒才響、且只在開倉路徑）。
5. **`weight` 目前是 0.30**（z=4.0 已撞 `VOL_Z_MAX_REDUCTION=0.7` 地板），
   即 `ALLOCATED_CAPITAL=1000` 實際只部署 **$300**。調整本金時要把這個乘數算進去。

---

## 實作時裁決（主線程，2026-08-20）

- `trader.py:316`（`_close_xyz`）**不加新告警**：該路徑既有 `tg.alert_error("平倉失敗",
  f"{coin} 無法取得中間價，請手動確認")`，已具 Telegram 可見性，再加一則是重複告警。
  只有 `sync.py` 的同步迴圈取價點是原本完全靜默的，故僅該處新增。
- `sync.py` 告警訊息由呼叫端傳入後果字串，區分「改用目標進場價概算本輪」（有 entry_px 後備）
  與「無後備價格，本輪跳過此標的」兩種實際後果；同時補一行 `logger.warning`（生產日誌等級
  是 INFO 寫死，warning 可見）。
- 控制流零改動：`mid_px` 的最終取值與原 `get_mid_price(...) or tgt_pos["entry_px"]` 完全等價。
