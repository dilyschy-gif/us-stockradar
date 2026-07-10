# US StockRadar — 美股市場熱度雷達

美股選股引擎加上市場熱度儀表板。系統把「進場準備度」與「新聞熱度」分開，
避免只因股票很熱門就把它當成買進候選。

## 線上頁面

- 美股雷達：<https://dilyschy-gif.github.io/us-stockradar/>
- 台股雷達：<https://dilyschy-gif.github.io/market-radar/>

兩個網站保留獨立資料管線，頁首可互相切換，不會因其中一個市場的排程失敗而拖累另一個。

## 資料流程

| 模組 | 用途 | 更新頻率 |
| --- | --- | --- |
| us_screener.py | SHA、N字右腳、BB收斂、基本面、籌碼替代、量能 | 美股收盤後每日 |
| us_chips_proxy.py | 主動型機構13F背景與Form 4人工複核連結 | 每週 |
| us_market_radar.py | Google News聲量、題材辨識、熱度×進場結構判讀、靜態網站 | 美股收盤後每日 |
| sheets_sync.py | 將掃描結果同步到Google Sheets | 美股收盤後每日 |

每日流程輸出：

- scan_result.csv
- us-data.json
- site/index.html
- site/data/latest.json
- site/data/history/YYYY-MM-DD.json

## 雷達判讀

entry_score沿用選股引擎的compositeScore：

| 面向 | 權重 |
| --- | ---: |
| 技術面 | 40 |
| 基本面 | 30 |
| 籌碼替代 | 15 |
| 量能 | 15 |

buzz_score則依近24小時新聞提及、時效與來源多樣性標準化為0–100。

| 熱度 | 進場條件 | 判讀 |
| --- | --- | --- |
| 高 | 非排除層且進場分≥60 | 熱度＋結構共振，優先研究 |
| 低 | 非排除層且進場分≥60 | 低熱早期候選 |
| 高 | 技術排除或波段新高 | 避免追價 |
| 低 | 進場分不足 | 持續追蹤 |

## 本機執行

    python -m pip install -r requirements.txt
    python -m unittest discover -s tests -v
    python us_screener.py
    python us_market_radar.py

離線測試儀表板，不抓新聞：

    python us_market_radar.py --no-news

## 自動更新

.github/workflows/us_screener.yml會在台灣時間06:00執行：

1. 跑單元測試。
2. 更新美股選股資料。
3. 選配同步Google Sheets。
4. 抓取新聞並產生美股市場熱度雷達。
5. 保存每日快照。
6. 發布到gh-pages。

第一次合併後，如GitHub Pages尚未啟用，請在Repository的
Settings → Pages將來源設為Deploy from a branch，分支選gh-pages / root。

## 資料限制

- 新聞熱度來自Google News公開RSS，不等於Reddit或Stocktwits社群貼文數。
- yfinance、13F與機構持股資料不是交易所即時法人流向。
- 13F具申報時差，僅作背景驗證。
- N字偵測為演算法近似，重要標的仍需人工看圖。
- 本工具是研究清單產生器，不構成投資建議。
