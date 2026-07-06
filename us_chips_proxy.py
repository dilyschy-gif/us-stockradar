# -*- coding: utf-8 -*-
"""
us_chips_proxy.py — 籌碼替代週更模組 v1.1
補齊 CLAUDE-US.md Stage 4 缺口：13F 機構持股趨勢（自動評分）+ 內部人交易（連結制）

資料來源：
  1. 機構持股趨勢：yfinance institutional_holders（13F 申報彙整）
     → 自動評分 instTrendScore(0-8)
  2. 內部人交易：OpenInsider 網站有機器人驗證機制，程式自動請求會被
     悄悄擋下、誤判為「無交易」（v1.0 測試中發現的真實問題）。
     v1.1 改為產生「已篩選好的官方查詢連結」，由指揮官親自點閱瀏覽器
     複核——完全避開爬蟲被擋的風險，也符合「重要標的人工複核」的
     決策紀律。

輸出：chips_proxy.csv + chips_proxy.json
      欄位：instTrendScore/instTrendDetail（自動）
           + openinsider_link/sec_edgar_link（人工複核連結）
      chipsProxyScore 目前 = instTrendScore（0-8），內部人不計入自動分數

用法：
    python us_chips_proxy.py          # 真實資料
    python us_chips_proxy.py --demo   # 模擬資料驗證機構趨勢評分邏輯
"""

import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

CFG = {
    "UNIVERSE_FILE": "universe.csv",
    "OUTPUT_CSV": "chips_proxy.csv",
    "OUTPUT_JSON": "chips_proxy.json",
    "INSIDER_LOOKBACK_DAYS": 90,
    "SLEEP_SEC": 2,  # 禮貌性間隔，避免被來源封鎖
    "OPENINSIDER_URL": (
        "http://openinsider.com/screener?s={ticker}"
        "&o=&pl=&ph=&ll=&lh=&fd=90&fdr=&td=0&tdr="
        "&fdlyl=&fdlyh=&daysago=&xp=1&xs=1"
        "&vl=&vh=&ocl=&och=&sic1=-1&sicl=100&sich=9999"
        "&grp=0&nfl=&nfh=&nil=&nih=&nol=&noh=&v2l=&v2h="
        "&oc2l=&oc2h=&sortcol=0&cnt=100&page=1"
    ),
}

TWN_TZ = timezone(timedelta(hours=8))


# ============================================================
# 1) 機構持股趨勢（13F 彙整）
# ============================================================
# v1.2(#7)：被動型巨頭名單——這些機構的持股隨指數資金流機械性增加，
# pctChange幾乎恆正，混入評分會讓大型股「人人有獎」（實測28檔有24檔滿分）。
# 剔除後只看主動型機構(Fidelity/T.Rowe/Wellington/Capital Group等)的增減持
PASSIVE_KEYWORDS = (
    "VANGUARD", "BLACKROCK", "ISHARES", "STATE STREET", "GEODE",
    "NORTHERN TRUST", "BANK OF NEW YORK", "CHARLES SCHWAB",
    "NORGES BANK", "INDEX", "ETF",
)


def _filter_active(holders: pd.DataFrame) -> pd.DataFrame:
    name_col = next((c for c in ["Holder", "holder", "organization"]
                     if c in holders.columns), None)
    if name_col is None:
        return holders  # 找不到名稱欄位就不過濾，退回原行為
    mask = ~holders[name_col].astype(str).str.upper().str.contains(
        "|".join(PASSIVE_KEYWORDS), regex=True, na=False)
    return holders[mask]


def score_inst_trend(holders: pd.DataFrame) -> tuple:
    """輸入 yfinance institutional_holders DataFrame。
    v1.2：先剔除被動型指數巨頭，只判讀「主動型機構」的增持vs減持家數。
    主動機構的加減碼才是有資訊含量的選擇（中長期背景驗證，非即時訊號）。"""
    if holders is None or holders.empty:
        return 0, "無13F資料"

    holders = _filter_active(holders)
    if len(holders) < 3:
        return 3, f"主動型機構樣本不足({len(holders)}家)-中性"

    # yfinance 欄位名稱在不同版本間有差異，做容錯
    pct_col = None
    for c in ["pctChange", "% Change", "pct_change"]:
        if c in holders.columns:
            pct_col = c
            break
    if pct_col is None:
        return 3, f"有{len(holders)}家機構資料但無增減欄位(中性)"

    chg = pd.to_numeric(holders[pct_col], errors="coerce").dropna()
    if chg.empty:
        return 3, "增減資料無法解析(中性)"

    inc = int((chg > 0.001).sum())
    dec = int((chg < -0.001).sum())
    total = len(chg)

    if inc >= total * 0.6:
        return 8, f"主動機構 {inc}/{total} 家增持(佈局趨勢)"
    if inc > dec:
        return 5, f"主動機構增持 {inc} vs 減持 {dec}(偏多)"
    if dec >= total * 0.6:
        return 0, f"⚠主動機構 {dec}/{total} 家減持"
    return 3, f"主動機構增持 {inc} vs 減持 {dec}(中性)"


def fetch_inst_trend(ticker: str) -> tuple:
    import yfinance as yf
    try:
        holders = yf.Ticker(ticker).institutional_holders
        return score_inst_trend(holders)
    except Exception as e:
        return 0, f"13F抓取失敗:{e}"


# ============================================================
# 2) 內部人交易（Form 4，經 OpenInsider）
# ============================================================
# ============================================================
# 2) 內部人交易（改為連結制，不爬蟲）
# ============================================================
# 原設計曾嘗試自動爬取 OpenInsider 表格，但該網站有機器人驗證機制，
# 一般程式請求會被擋下、回傳空頁面而非報錯，導致每支股票都誤判為
# 「無交易」——這種靜默失敗比不做還危險，因此改為連結制：
# 程式產生正確篩選好的連結，由指揮官用瀏覽器親自點閱複核，
# 完全避開爬蟲被擋的問題，且符合「重要標的人工複核」的決策紀律。

SEC_EDGAR_FORM4_URL = (
    "https://www.sec.gov/cgi-bin/browse-edgar"
    "?action=getcompany&CIK={ticker}&type=4&dateb=&owner=include&count=40"
)


def build_insider_links(ticker: str) -> dict:
    return {
        "openinsider_link": CFG["OPENINSIDER_URL"].format(ticker=ticker),
        "sec_edgar_link": SEC_EDGAR_FORM4_URL.format(ticker=ticker),
    }


# ============================================================
# 主流程
# ============================================================
def run(demo: bool = False) -> pd.DataFrame:
    base = os.path.dirname(os.path.abspath(__file__))
    # v1.2：寬容版母池讀取——新增股票只需填代號(對齊us_screener)
    universe = pd.read_csv(os.path.join(base, CFG["UNIVERSE_FILE"]), dtype=str)
    for col in ["ticker", "name", "sector", "theme"]:
        if col not in universe.columns:
            universe[col] = ""
    universe = universe.fillna("")
    universe["ticker"] = universe["ticker"].str.strip().str.upper()
    universe = universe[universe["ticker"] != ""].drop_duplicates(subset="ticker", keep="first")

    rows = []
    for _, r in universe.iterrows():
        tk = r["ticker"]
        if demo:
            i_s, i_d = score_inst_trend(_demo_holders(tk))
        else:
            i_s, i_d = fetch_inst_trend(tk)
            time.sleep(CFG["SLEEP_SEC"])
        links = build_insider_links(tk)
        rows.append({
            "ticker": tk, "name": r["name"] or tk,
            "instTrendScore": i_s, "instTrendDetail": i_d,
            "chipsProxyScore": i_s,   # 內部人改連結制，不計入自動分數
            "openinsider_link": links["openinsider_link"],
            "sec_edgar_link": links["sec_edgar_link"],
        })

    out = pd.DataFrame(rows).sort_values("chipsProxyScore", ascending=False)
    ts = datetime.now(TWN_TZ).strftime("%Y-%m-%d %H:%M")
    out.to_csv(os.path.join(base, CFG["OUTPUT_CSV"]), index=False, encoding="utf-8-sig")
    with open(os.path.join(base, CFG["OUTPUT_JSON"]), "w", encoding="utf-8") as f:
        json.dump({"updated": ts, "source": "us_chips_proxy v1.1(內部人改連結制)",
                   "results": out.to_dict(orient="records")},
                  f, ensure_ascii=False, indent=1)
    return out


# ============================================================
# Demo 模擬資料（驗證評分邏輯）
# ============================================================
def _demo_holders(ticker: str) -> pd.DataFrame:
    rng = np.random.default_rng(sum(ord(c) for c in ticker))
    scenario = sum(ord(c) for c in ticker) % 3
    n = 10
    if scenario == 0:    # 機構佈局中
        chg = rng.uniform(0.01, 0.15, n) * np.where(rng.random(n) < 0.8, 1, -1)
    elif scenario == 1:  # 機構撤退
        chg = rng.uniform(0.01, 0.15, n) * np.where(rng.random(n) < 0.25, 1, -1)
    else:                # 中性
        chg = rng.uniform(-0.05, 0.05, n)
    return pd.DataFrame({"Holder": [f"Fund{i}" for i in range(n)], "pctChange": chg})


if __name__ == "__main__":
    result = run(demo="--demo" in sys.argv)
    print(result.to_string(index=False))
    print(f"\n完成：{len(result)} 支 → {CFG['OUTPUT_CSV']} / {CFG['OUTPUT_JSON']}")
