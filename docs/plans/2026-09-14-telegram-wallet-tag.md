# Telegram 訊息加錢包識別（wallet tag）

日期：2026-09-14
狀態：待實作

## 背景與動機

同一個 leader 將由兩顆 follower 錢包跟單（現有 $1,000 錢包＋新的 $10,000 錢包），
兩台各自部署，但沿用**同一組 Telegram bot 與 chat**。目前所有 TG 訊息都沒有錢包識別
（`src/telegram.py` 的通知函式都不帶地址），兩台一起發訊息時無法分辨來源。

本 plan 只做一件事：**每一則 TG 訊息前面加上錢包識別前綴**。不動任何交易邏輯、
不動 dedup / retry 語意、不動其他檔案。

## 命名慣例（2026-09-14 使用者定案）

`<機器名>｜Multi-Asset Copy｜<錢包末四碼>`

| 機器 | WALLET_LABEL |
|---|---|
| 現有 $1,000（Lightsail 3.113.62.242） | `Base｜Multi-Asset Copy｜0111` |
| 新 $10,000（待建） | `Jim｜Multi-Asset Copy｜XXXX`（XXXX = 新錢包地址末四碼） |

## 設計

- 單一注入點：`src/telegram.py::_send`。所有通知函式都經它送出，所以在這裡加前綴
  就涵蓋全部訊息（含 alert_*、notify_*、同步摘要、啟停通知）。
- 識別內容：新增設定 `WALLET_LABEL`（人類可讀名稱，如 `主帳1k`）。
  - 有設 `WALLET_LABEL` → 用它。
  - 沒設 → 用 `WALLET_ADDRESS` 縮寫 `0x1A1d…0111`（前 6 字＋`…`＋後 4 字）。
  - 兩者皆空 → 不加前綴，訊息與現在完全相同。
- 前綴格式：`[識別]` 自成一行，接換行後才是原訊息（2026-09-14 使用者看過實際訊息後定案），例如
  ```
  [Base｜Multi-Asset Copy｜0111]
  【系統】跟單機器人已啟動 🤖
  ```
  識別文字經 `html.escape`（訊息用 `parse_mode=HTML`，label 含 `<`、`&` 不能破壞解析）。
- 前綴在 module 載入時算一次（`_WALLET_TAG` 常數），`_send` 每次直接用。
- `dedup_key` 不變（呼叫端傳什麼就是什麼），去重與失敗抑制行為完全不受影響。
- 前綴加在 dedup 判斷之後、送出之前；retry 迴圈內每次送的是同一份已加前綴的 text。

## Task 1 @inline — 實作前綴 + 設定 + 測試

改動限於以下四個檔案，**不得動其他檔案**：

1. `src/config.py`：在 `WALLET_ADDRESS = ...`（第 35 行）之後新增
   ```python
   # Telegram 訊息前綴用的錢包識別（多台各跑一顆錢包、共用同一個 bot 時用來分辨來源）。
   # 留空則用 WALLET_ADDRESS 縮寫；兩者皆空則不加前綴。
   WALLET_LABEL = _env_str("WALLET_LABEL", "")
   ```
2. `src/telegram.py`：
   - `from .config import ...` 加入 `WALLET_ADDRESS, WALLET_LABEL`。
   - 在 `_send` 之前新增：
     ```python
     def _wallet_tag() -> str:
         """訊息前綴用的錢包識別：WALLET_LABEL 優先，否則 WALLET_ADDRESS 縮寫
         （0x1A1d…0111）；兩者皆空回空字串（不加前綴）。"""
         if WALLET_LABEL:
             return WALLET_LABEL
         if WALLET_ADDRESS and len(WALLET_ADDRESS) > 10:
             return f"{WALLET_ADDRESS[:6]}…{WALLET_ADDRESS[-4:]}"
         return WALLET_ADDRESS

     _WALLET_TAG = _wallet_tag()
     ```
   - `_send` 內，在 dedup 區塊之後、`url = _API.format(...)` 之前加入：
     ```python
     if _WALLET_TAG:
         text = f"[{_html.escape(_WALLET_TAG)}] {text}"
     ```
     其餘一行不改。
3. `.env.example`：在 `WALLET_ADDRESS=...`（第 15 行）之後新增
   ```
   # Telegram 訊息前綴用的錢包名稱（多台共用同一個 bot 時分辨來源），例如 主帳1k / 新機10k。
   # 留空則自動用 WALLET_ADDRESS 縮寫（0x1A1d…0111）。
   WALLET_LABEL=
   ```
4. 新增 `tests/test_wallet_tag.py`，比照 `tests/test_alert_retry.py` 的寫法
   （`REAL_SEND = telegram._send` 在 conftest mute 之前取得；`_arm` 設 token/chat/清 dedup；
   monkeypatch `telegram.requests.post` 捕捉 `json["text"]`）。至少四個測試：
   - `_WALLET_TAG` patch 為 `"新機10k"` → 送出 text 為 `[新機10k] hi`。
   - `_WALLET_TAG` patch 為 `""` → 送出 text 為 `hi`（與現行完全相同）。
   - `_WALLET_TAG` patch 為 `"a<b&c"` → 送出 text 為 `[a&lt;b&amp;c] hi`。
   - `_wallet_tag()` 推導：monkeypatch `telegram.WALLET_LABEL=""`、
     `telegram.WALLET_ADDRESS="0x1A1d0000000000000000000000000000000000111"`（任一 ≥ 11 字的字串即可）
     → 回傳 `0x1A1d…0111`；`WALLET_LABEL="X"` 時回 `X`；兩者皆空回 `""`。

### 驗收指令（逐條要有輸出）

```
cd /Users/jim/projects/hl-copytrader
.venv/bin/python -m pytest tests/ -q          # 全綠；數量 = 基線 + 新增測試數
git status --short                            # 只出現上述 4 個檔案（3 修改 + 1 新增）
git diff --stat                               # telegram.py / config.py / .env.example 三檔
```

### 不在範圍（明確不做）

- 不把 tag 傳進各通知函式簽章、不改任何 `notify_*` / `alert_*` 的內容格式。
- 不動 dedup / `_recent_failed` / retry 邏輯。
- 不動 `main.py`、`deploy/`、其他 `src/` 模組。
- 不 commit、不部署（由使用者決定）。
