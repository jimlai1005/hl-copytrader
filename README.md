# hl-copytrader

Hyperliquid 跟單機器人 —— 以「**掛單鏡像（open-orders mirror）+ 部位安全網**」的方式，按比例複製目標交易員在 Hyperliquid（含 xyz 美股永續）的操作。

支援動態頻率（美股活躍時段每分鐘、其餘可設每小時/每 5 分鐘）、波動度去槓桿、抗單保護、Telegram 通知、systemd 部署。

---

## ⚠️ 風險聲明（請務必先讀）

> **這是會用真錢下單的程式，永續合約槓桿交易，可能在短時間內造成重大甚至全部本金虧損。**

- 本專案僅供學習與個人使用，**不構成任何投資建議**，作者不對任何虧損負責。
- 跟單的本質是放大他人的操作 —— **目標交易員賠錢時，你也會等比例賠錢**。對方若爆倉/抗單，你也會被一起拖下水。
- **請先用 `--dry-run` 乾跑充分測試**，確認下單大小、方向、保證金都符合預期，再開 `LIVE_TRADING=true`。
- 已知限制與殘餘風險請見 [docs/superpowers/plans/](docs/superpowers/plans/) 與程式內註解（例如：API 瞬間缺資料、xyz 交易時段、首次接手的市價成交、目標瀕臨清算時的有效槓桿暴衝等）。
- 建議只投入「賠光也不影響生活」的金額，並設定 `MAX_DRAWDOWN_PCT` 與（選用）`MAX_TARGET_LEVERAGE` 保護。

---

## 策略簡介

每次同步（頻率見下）會做兩件事：

1. **掛單對帳（diff）**：抓目標交易員當前掛單，依比例縮放成自己的掛單，只動「不一樣」的（相同的保留不動）。能改價/量就用 `modify` 就地改，否則先取消再重掛。
2. **部位安全網**：比對自己與目標的實際部位，跟上目標用市價造成的部分平/建倉（全平→全平、部分平→等比例減、部分建→跟著建）。

**跟單大小** = `(你的本金 × 資金使用率 × 倉位權重) / 目標帳戶淨值`，自動反映目標的有效槓桿（而非照抄名目槓桿）。名目槓桿一律設標的最大值（cross；xyz/onlyIsolated 自動改 isolated），只為節省保證金、不影響倉位大小。

---

## 安裝與設定

需求：Python 3.9+

```bash
git clone git@github.com:jimlai1005/hl-copytrader.git
cd hl-copytrader
pip install -r requirements.txt

cp .env.example .env
# 編輯 .env，至少填好下面「必填」欄位
```

### 必填 / 關鍵設定（.env）

| 變數 | 說明 |
|------|------|
| `TARGET_TRADER_ADDRESS` | 要跟單的目標交易員主帳戶地址 |
| `WALLET_PRIVATE_KEY` | 簽名下單用的私鑰（建議用 Hyperliquid API/agent 錢包的私鑰） |
| `WALLET_ADDRESS` | **你的主帳戶地址（實際持有 USDC 的那個）—— 不是 agent 地址！** |
| `ALLOCATED_CAPITAL` | 跟單資金 (USDC)。設 `<= 0` 則自動用帳戶當前權益 |
| `LIVE_TRADING` | `false`（預設，只監控不下單）/ `true`（真實下單） |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram 通知（選填） |

其餘可調參數（資金使用率、波動權重、抗單保護、同步頻率、size 容忍度、目標槓桿上限、通知開關等）都在 `.env.example` 內有完整中文說明。

> 🔒 `.env` 已被 `.gitignore`，**永遠不會被提交**。請勿把私鑰/Token 放進任何會進版控的檔案。

---

## 執行

```bash
python main.py --status            # 只看目標當前部位與掛單，不下單
python main.py --once --dry-run    # 乾跑一次完整同步（強烈建議先跑這個）
python main.py --dry-run           # 持續乾跑（不下單）
python main.py                     # 正式運行（依 .env 的 LIVE_TRADING 決定是否真下單）
python main.py --orders-only       # 只鏡像掛單、不跑部位安全網（溫和接手用）
```

**同步頻率**：美股活躍時段（盤前 04:00–16:00 ET）每分鐘；其餘依 `OFFHOURS_SYNC_MODE`（`hourly` 每小時 :55 / `5min` 每 5 分鐘）。

---

## 測試

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q
```

測試全離線（mock 掉網路與交易所），涵蓋下單核心的 characterization、DEX 查詢失敗保護、reduce-only/modify/safety-net 等邊界。CI 會在每次 push 自動跑。

---

## 架構

| 模組 | 職責 |
|------|------|
| `main.py` | 進入點、排程迴圈、每輪同步編排、回撤保護 |
| `src/config.py` | 從 `.env` 載入所有設定 |
| `src/monitor.py` | 查詢目標/自己的帳戶狀態、掛單、權益、近期高點（Hyperliquid REST） |
| `src/orders.py` | 掛單對帳（diff / modify / cancel+place）+ 呼叫部位安全網 |
| `src/sync.py` | 部位安全網（市價跟上目標部位）+ 跟單比例計算 |
| `src/trader.py` | `Trader` 類別：實際下單/改單/取消/開平倉（包裝 Hyperliquid SDK） |
| `src/instrument.py` | 無狀態工具：幣名/DEX、現貨判斷、size 進位、meta 查詢、錯誤路由 |
| `src/weight.py` | 倉位權重（對目標單日盈虧算 Z-Score 去槓桿）+ 帳戶波動統計 |
| `src/protection.py` | 抗單保護（對目標持倉時間算 Z-Score） |
| `src/market.py` | 美股交易時段判斷 |
| `src/telegram.py` | Telegram 通知（系統/警告/同步/平倉一律發；掛單/開倉/波動可開關） |

---

## 部署（systemd）

`deploy/` 下有腳本（`setup.sh` 互動式安裝、`update.sh` 更新重啟、`hl-copytrader.service`）。在 VPS 上：

```bash
sudo bash deploy/setup.sh      # 首次安裝（含 .env 設定精靈、venv、systemd）
sudo bash deploy/update.sh     # 拉新版 + 重啟
journalctl -u hl-copytrader -f # 看即時 log
```

---

## 免責

本軟體以「現狀」提供，不附任何明示或暗示的擔保。使用本軟體進行交易的一切風險與後果由使用者自行承擔。
