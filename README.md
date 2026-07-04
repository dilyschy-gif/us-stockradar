# US StockRadar v1.0 — 部署說明

依 CLAUDE-US.md 六階段漏斗實作的美股選股程式。

## 檔案清單

| 檔案 | 用途 |
|------|------|
| `us_screener.py` | 主程式：SHA 訊號 + N 理論右腳偵測 + BB 收斂 + 四面向評分 |
| `universe.csv` | 母池（22 支範例，七大板塊 × 主題標籤，可自行增刪） |
| `sheets_sync.py` | 掃描結果寫入 Google Sheets「US_WarRoom」 |
| `us_screener.yml` | GitHub Actions 排程（放到 `.github/workflows/`） |

## 部署步驟（一次一步）

### 第一步：本機測試（今天做這步就好）
```bash
pip install yfinance pandas numpy
python us_screener.py          # 真實資料掃描
python us_screener.py --demo   # 合成資料驗證邏輯
```
輸出：`scan_result.csv`（Excel 可開）+ `us-data.json`（給網頁用）

### 第二步：GitHub 上架
1. 建新 repo（或用現有 twstock repo 開新資料夾）
2. 上傳 4 個檔案，`us_screener.yml` 放到 `.github/workflows/`
3. 手動觸發一次（Actions → Run workflow）驗證

### 第三步：Sheets 同步（選配）
1. 建立 Google Sheet「US_WarRoom」，複製試算表 ID
2. GCP Service Account 金鑰 JSON → GitHub Secrets 設 `GSA_JSON`
3. Secrets 設 `US_SHEET_ID`
4. 把 Service Account email 加入 Sheet 編輯者

### 第四步：觀察一週再說
讓系統跑 5 個交易日，核對訊號合理性後，才考慮接 BB-8 早盤報告或 Cloudflare Pages。

## 評分架構（總分 100）

| 面向 | 權重 | 內容 |
|------|------|------|
| techScore | 40 | SHA=BUY(12) + N理論右腳★(16) + BB收斂(8) + 站上MA20(4) |
| fundScore | 30 | 營收YoY>15%(12) + EPS成長>10%(12) + 毛利率>30%(6) |
| chipsScore | 15 | 空單佔比低(7) + 機構持股>60%(8) ※13F/內部人待週更模組 |
| volScore | 15 | 日均額>$20M(8) + 回檔量縮沉澱(7) |

## 分層邏輯（tier）

- `★重點觀察(右腳+SHA多)`：N理論右腳即將形成 + SHA=BUY → 最優先
- `觀察層-右腳成形中`：右腳區但 SHA 未翻多
- `觀察層`：綜合分 ≥60
- `排除-波段新高(中後段)`：60日新高 = 已大漲，不符主升段起漲目標
- `排除-空頭結構`：跌破 A 點，N 結構失效（PYPL 教訓）
- `排除-量能不足`：日均成交額 < $20M（量能鐵律美股版）

## 參數調整

全部集中在 `us_screener.py` 開頭的 `CFG`，與 StockRadar Pro 同風格。
常調項目：`N_MIN_LEG_PCT`（左腳最小漲幅）、`N_NEAR_C_PCT`（右腳區寬度）、
`MIN_AVG_DOLLAR_VOL`（量能門檻）、`BB_SQUEEZE_PCT`（收斂判定）。

## 已知限制

1. **籌碼替代不完整**：目前僅空單佔比 + 機構持股比（yfinance 即時可得）；
   13F 趨勢與內部人 Form 4 需另建週更模組（SEC EDGAR / OpenInsider）。
2. **分析師修正**：yfinance 免費資料無預估修正歷史，Stage 2 此項暫缺。
3. **N 理論偵測是演算法近似**：swing 偵測參數化，重要標的仍應人工看圖確認
   ——程式是初篩，不是決策。
