# -*- coding: utf-8 -*-
"""
US StockRadar — 美股選股程式 v1.0
依據 CLAUDE-US.md 六階段漏斗設計
架構：母池 → 技術面(SHA + N理論右腳 + BB收斂) → 基本面動能 → 籌碼替代 → 綜合評分

作戰室：Dilys(指揮官) / R2(策略參謀) / BB-8(情報官)

用法:
    python us_screener.py                # 讀 universe.csv，輸出 scan_result.csv / .json
    python us_screener.py --demo         # 用合成資料測試指標邏輯（無網路環境）
"""

import json
import sys
import os
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

# ============================================================
# CFG — 全部參數集中管理（對齊 StockRadar Pro 風格）
# ============================================================
CFG = {
    "UNIVERSE_FILE": "universe.csv",
    "OUTPUT_CSV": "scan_result.csv",
    "OUTPUT_JSON": "us-data.json",
    "LOOKBACK_PERIOD": "1y",       # 抓一年日線（v1.1修正：yfinance的"250d"是日曆日≈170根K，改用"1y"）

    # --- SHA (Smoothed Heiken Ashi) ---
    "SHA_LEN1": 10,                # 第一次 EMA 平滑
    "SHA_LEN2": 10,                # HA 後第二次 EMA 平滑

    # --- 布林通道 ---
    "BB_PERIOD": 20,
    "BB_STD": 2.0,
    "BB_SQUEEZE_PCT": 25,          # 帶寬位於近120日 25 百分位以下 = 收斂

    # --- N 理論擺動偵測 ---
    "SWING_WINDOW": 5,             # 前後5日高低點判定 swing
    "N_MIN_LEG_PCT": 12,           # A→B 至少漲 12% 才算有效左腳
    "N_C_RETRACE_MIN": 0.30,       # C 回檔至少 30%（太淺不算回檔）
    "N_C_RETRACE_MAX": 0.80,       # C 回檔不超過 80%（太深結構破壞）
    "N_NEAR_C_PCT": 8,             # 現價距 C 點 8% 內 = 「右腳即將形成」區
    "N_B_DOMINANCE_BARS": 90,      # v1.1：B候選若在此範圍內存在更高的前swing高點→跳過
                                   # （防止C後的反彈小高點遮蔽真正的主結構B）

    # --- 新鮮突破豁免（#5，預設關閉，由指揮官決定是否啟用） ---
    "TRACK_FRESH_BREAKOUT": False, # True=剛突破B且漲幅未超過B之上FRESH_BREAKOUT_MAX_PCT者
    "FRESH_BREAKOUT_MAX_PCT": 5,   #      不套用波段新高排除，改列「突破確認」層

    # --- 排除條件（波段新高 = 中後段，不符主升段起漲目標） ---
    "EXCLUDE_NEW_HIGH_DAYS": 60,   # 60日新高視為已突破
    "NEW_HIGH_BUFFER_PCT": 2,      # 距60日高 2% 內也算

    # --- 量能 ---
    "VOL_MA": 20,
    "MIN_AVG_DOLLAR_VOL": 20e6,    # 日均成交額 2000萬美元（美股版量能鐵律）

    # --- 基本面門檻（Stage 2） ---
    "REV_GROWTH_MIN": 0.15,        # 營收 YoY > 15%
    "EARNINGS_GROWTH_MIN": 0.10,

    # --- 評分權重（總分100） ---
    "W_TECH": 40,
    "W_FUND": 30,
    "W_CHIPS": 15,
    "W_VOL": 15,
}

TWN_TZ = timezone(timedelta(hours=8))


# ============================================================
# 技術指標模組
# ============================================================
def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def smoothed_heiken_ashi(df: pd.DataFrame, len1: int, len2: int) -> pd.DataFrame:
    """SHA：先 EMA 平滑 OHLC → 計算 Heiken Ashi → 再 EMA 平滑 HA。
    回傳含 sha_open / sha_close / sha_signal 的 DataFrame。"""
    o = ema(df["Open"], len1)
    h = ema(df["High"], len1)
    l = ema(df["Low"], len1)
    c = ema(df["Close"], len1)

    ha_close = (o + h + l + c) / 4.0
    ha_open = ha_close.copy()
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2.0

    sha_open = ema(ha_open, len2)
    sha_close = ema(ha_close, len2)

    out = pd.DataFrame(index=df.index)
    out["sha_open"] = sha_open
    out["sha_close"] = sha_close
    out["sha_signal"] = np.where(sha_close > sha_open, "BUY", "SELL")
    return out


def bollinger(df: pd.DataFrame, period: int, nstd: float) -> pd.DataFrame:
    mid = df["Close"].rolling(period).mean()
    std = df["Close"].rolling(period).std()
    out = pd.DataFrame(index=df.index)
    out["bb_mid"] = mid
    out["bb_up"] = mid + nstd * std
    out["bb_dn"] = mid - nstd * std
    out["bb_width"] = (out["bb_up"] - out["bb_dn"]) / mid * 100
    # 帶寬在近120日的百分位（小 = 收斂）
    out["bb_width_pctile"] = (
        out["bb_width"].rolling(120, min_periods=60)
        .apply(lambda w: (w <= w.iloc[-1]).mean() * 100, raw=False)
    )
    return out


def find_swings(df: pd.DataFrame, window: int):
    """簡易 fractal：回傳 (swing_lows, swing_highs) 各為 [(idx, price)]。"""
    lows, highs = [], []
    lo = df["Low"].values
    hi = df["High"].values
    n = len(df)
    for i in range(window, n - window):
        if lo[i] == lo[i - window : i + window + 1].min():
            lows.append((i, lo[i]))
        if hi[i] == hi[i - window : i + window + 1].max():
            highs.append((i, hi[i]))
    return lows, highs


def n_theory_status(df: pd.DataFrame) -> dict:
    """N 理論結構偵測：找最近的 A(低)→B(高)→C(回檔低) 結構。
    回傳 A/B/C 價位、目標價、階段判定。"""
    res = {
        "n_stage": "無結構",
        "A": None, "B": None, "C": None,
        "n_target": None, "dist_to_C_pct": None,
    }
    lows, highs = find_swings(df, CFG["SWING_WINDOW"])
    if not lows or not highs:
        return res

    price = float(df["Close"].iloc[-1])

    # 找最近一組：A 低點 → 之後的 B 高點 → 之後的 C 低點(可為進行中)
    for bi in range(len(highs) - 1, -1, -1):
        b_idx, b_px = highs[bi]

        # v1.1修正(#3)：B支配性檢查——若候選B之前N_B_DOMINANCE_BARS根K內
        # 存在更高的swing高點，代表這只是回檔後的反彈小高，跳過，
        # 讓迴圈往前找到真正的主結構B（否則主結構會被反彈波遮蔽）
        dominated = any(
            (b_idx - h_idx) <= CFG["N_B_DOMINANCE_BARS"] and h_px > b_px
            for h_idx, h_px in highs
            if h_idx < b_idx
        )
        if dominated:
            continue

        # B 之前最近的低點 = A
        prior_lows = [x for x in lows if x[0] < b_idx]
        if not prior_lows:
            continue
        a_idx, a_px = prior_lows[-1]
        leg_pct = (b_px - a_px) / a_px * 100
        if leg_pct < CFG["N_MIN_LEG_PCT"]:
            continue

        # v1.1修正(#4)：C = B之後「所有K棒的最低價」（含最近5天未確認區段）
        # 原版取最後一個swing低點，會漏掉近期急跌，導致dist_to_C與retrace失真、
        # 甚至價格已破位仍亮「右腳即將形成★」
        seg = df["Low"].iloc[b_idx + 1 :]
        if seg.empty:
            continue
        c_px = float(seg.min())

        retrace = (b_px - c_px) / (b_px - a_px) if b_px > a_px else 1.0
        res.update({"A": round(a_px, 2), "B": round(b_px, 2), "C": round(c_px, 2)})

        if c_px <= a_px:
            res["n_stage"] = "破A點-結構失效(空頭)"
            return res
        if retrace < CFG["N_C_RETRACE_MIN"]:
            res["n_stage"] = "回檔過淺-觀察"
        elif retrace > CFG["N_C_RETRACE_MAX"]:
            res["n_stage"] = "回檔過深-結構弱"
        else:
            dist_c = abs(price - c_px) / c_px * 100
            res["dist_to_C_pct"] = round(dist_c, 1)
            if price > b_px:
                res["n_stage"] = "已突破B-右腳進行中"
            elif dist_c <= CFG["N_NEAR_C_PCT"]:
                res["n_stage"] = "右腳即將形成★"   # ← 目標區
            else:
                res["n_stage"] = "C-B之間整理"
        res["n_target"] = round(b_px + (b_px - a_px), 2)
        return res
    return res


def is_new_high(df: pd.DataFrame) -> bool:
    """波段新高判定（排除條件：已大漲 = 中後段）。"""
    days = CFG["EXCLUDE_NEW_HIGH_DAYS"]
    if len(df) < days:
        return False
    recent_high = df["High"].iloc[-days:].max()
    price = df["Close"].iloc[-1]
    return price >= recent_high * (1 - CFG["NEW_HIGH_BUFFER_PCT"] / 100)


# ============================================================
# 評分引擎（對齊 StockRadar Pro：tech/fund/chips/vol → composite）
# ============================================================
def score_tech(sha_sig: str, n_stage: str, bb_pctile: float, above_ma20: bool) -> tuple:
    s, detail = 0, []
    if sha_sig == "BUY":
        s += 12; detail.append("SHA=BUY")
    if n_stage == "右腳即將形成★":
        s += 16; detail.append("N理論右腳區")
    elif n_stage == "C-B之間整理":
        s += 8; detail.append("N結構完整整理中")
    elif "空頭" in n_stage or "失效" in n_stage:
        s -= 10; detail.append("⚠N結構失效")
    if bb_pctile is not None and bb_pctile <= CFG["BB_SQUEEZE_PCT"]:
        s += 8; detail.append("BB收斂")
    if above_ma20:
        s += 4; detail.append("站上MA20")
    return max(0, min(s, CFG["W_TECH"])), "、".join(detail)


def score_fund(info: dict) -> tuple:
    s, detail = 0, []
    rg = info.get("revenueGrowth")
    eg = info.get("earningsGrowth")
    margins = info.get("grossMargins")
    if rg is not None:
        if rg >= CFG["REV_GROWTH_MIN"]:
            s += 12; detail.append(f"營收YoY {rg*100:.0f}%")
        elif rg > 0:
            s += 5; detail.append(f"營收YoY {rg*100:.0f}%(低於門檻)")
        else:
            detail.append(f"⚠營收YoY {rg*100:.0f}%")
    if eg is not None and eg >= CFG["EARNINGS_GROWTH_MIN"]:
        s += 12; detail.append(f"EPS成長 {eg*100:.0f}%")
    if margins is not None and margins > 0.30:
        s += 6; detail.append(f"毛利率 {margins*100:.0f}%")
    return min(s, CFG["W_FUND"]), "、".join(detail) or "無資料"


def score_chips_proxy(info: dict) -> tuple:
    """籌碼替代：以 yfinance 可得的空單/機構持股比先做，
    13F趨勢與內部人交易由週更流程補（見 chips_proxy 欄位說明）。"""
    s, detail = 0, []
    short_pct = info.get("shortPercentOfFloat")
    inst_pct = info.get("heldPercentInstitutions")
    if short_pct is not None:
        if short_pct < 0.03:
            s += 7; detail.append(f"空單佔比 {short_pct*100:.1f}%(低)")
        elif short_pct > 0.10:
            s -= 5; detail.append(f"⚠空單佔比 {short_pct*100:.1f}%(高)")
        else:
            s += 3; detail.append(f"空單佔比 {short_pct*100:.1f}%")
    if inst_pct is not None and inst_pct > 0.60:
        s += 8; detail.append(f"機構持股 {inst_pct*100:.0f}%")
    return max(0, min(s, CFG["W_CHIPS"])), "、".join(detail) or "無資料"


def score_vol(df: pd.DataFrame) -> tuple:
    s, detail = 0, []
    dollar_vol = (df["Close"] * df["Volume"]).rolling(CFG["VOL_MA"]).mean().iloc[-1]
    if pd.isna(dollar_vol):
        return 0, "資料不足"
    if dollar_vol < CFG["MIN_AVG_DOLLAR_VOL"]:
        return 0, f"⚠日均額 {dollar_vol/1e6:.0f}M 低於鐵律門檻"
    s += 8
    detail.append(f"日均額 {dollar_vol/1e6:.0f}M")
    # 回檔量縮加分（近5日均量 < 20日均量 = 籌碼沉澱）
    v5 = df["Volume"].iloc[-5:].mean()
    v20 = df["Volume"].iloc[-20:].mean()
    if v5 < v20 * 0.85:
        s += 7; detail.append("回檔量縮(沉澱)")
    return min(s, CFG["W_VOL"]), "、".join(detail)


# ============================================================
# 主流程
# ============================================================
def analyze_one(ticker: str, name: str, sector: str, theme: str,
                df: pd.DataFrame, info: dict) -> dict:
    if df is None or len(df) < 120:
        return {"ticker": ticker, "name": name, "status": "資料不足"}

    sha = smoothed_heiken_ashi(df, CFG["SHA_LEN1"], CFG["SHA_LEN2"])
    bb = bollinger(df, CFG["BB_PERIOD"], CFG["BB_STD"])
    nres = n_theory_status(df)
    new_high = is_new_high(df)

    price = float(df["Close"].iloc[-1])
    ma20 = float(df["Close"].rolling(20).mean().iloc[-1])
    sha_sig = sha["sha_signal"].iloc[-1]
    bb_pctile = bb["bb_width_pctile"].iloc[-1]
    bb_pctile = None if pd.isna(bb_pctile) else float(bb_pctile)

    t_s, t_d = score_tech(sha_sig, nres["n_stage"], bb_pctile, price > ma20)
    f_s, f_d = score_fund(info)
    c_s, c_d = score_chips_proxy(info)
    v_s, v_d = score_vol(df)
    composite = t_s + f_s + c_s + v_s

    # 分層判定
    # v1.1(#5)：新鮮突破豁免——剛突破B且距B不超過FRESH_BREAKOUT_MAX_PCT，
    # 結構健康者不套用波段新高排除（預設關閉，CFG開關控制）
    fresh_breakout = (
        CFG["TRACK_FRESH_BREAKOUT"]
        and nres["n_stage"] == "已突破B-右腳進行中"
        and nres["B"] is not None
        and price <= nres["B"] * (1 + CFG["FRESH_BREAKOUT_MAX_PCT"] / 100)
    )
    if fresh_breakout:
        tier = "突破確認-右腳進行中"
    elif new_high:
        tier = "排除-波段新高(中後段)"
    elif "失效" in nres["n_stage"] or "空頭" in nres["n_stage"]:
        tier = "排除-空頭結構"
    elif v_s == 0:
        tier = "排除-量能不足"
    elif nres["n_stage"] == "右腳即將形成★" and sha_sig == "BUY":
        tier = "★重點觀察(右腳+SHA多)"
    elif nres["n_stage"] == "右腳即將形成★":
        tier = "觀察層-右腳成形中"
    elif composite >= 60:
        tier = "觀察層"
    else:
        tier = "母池追蹤"

    return {
        "ticker": ticker, "name": name, "sector": sector, "theme": theme,
        "price": round(price, 2),
        "sha_signal": sha_sig,
        "n_stage": nres["n_stage"],
        "A": nres["A"], "B": nres["B"], "C": nres["C"],
        "n_target": nres["n_target"],
        "dist_to_C_pct": nres["dist_to_C_pct"],
        "bb_width_pctile": None if bb_pctile is None else round(bb_pctile, 0),
        "new_high_60d": new_high,
        "techScore": t_s, "fundScore": f_s,
        "chipsScore": c_s, "volScore": v_s,
        "compositeScore": composite,
        "tier": tier,
        "techDetail": t_d, "fundDetail": f_d,
        "chipsDetail": c_d, "volDetail": v_d,
        "status": "OK",
    }


def run_scan(demo: bool = False) -> pd.DataFrame:
    base = os.path.dirname(os.path.abspath(__file__))
    upath = os.path.join(base, CFG["UNIVERSE_FILE"])
    universe = pd.read_csv(upath, dtype=str).fillna("")

    rows = []
    if demo:
        for _, r in universe.iterrows():
            df, info = make_demo_data(r["ticker"])
            rows.append(analyze_one(r["ticker"], r["name"], r["sector"], r["theme"], df, info))
    else:
        import yfinance as yf
        for _, r in universe.iterrows():
            try:
                tk = yf.Ticker(r["ticker"])
                df = tk.history(period=CFG["LOOKBACK_PERIOD"], auto_adjust=True)
                info = {}
                try:
                    info = tk.info or {}
                except Exception:
                    pass
                rows.append(analyze_one(r["ticker"], r["name"], r["sector"], r["theme"], df, info))
            except Exception as e:
                rows.append({"ticker": r["ticker"], "name": r["name"],
                             "status": f"錯誤:{e}"})

    out = pd.DataFrame(rows)
    if "compositeScore" in out.columns:
        tier_rank = {"★重點觀察(右腳+SHA多)": 0, "突破確認-右腳進行中": 1,
                     "觀察層-右腳成形中": 2, "觀察層": 3, "母池追蹤": 4}
        out["_tr"] = out["tier"].map(lambda t: tier_rank.get(t, 9))  # 排除類=9墊底
        out = out.sort_values(["_tr", "compositeScore"],
                              ascending=[True, False]).drop(columns="_tr")

    ts = datetime.now(TWN_TZ).strftime("%Y-%m-%d %H:%M")
    out.to_csv(os.path.join(base, CFG["OUTPUT_CSV"]), index=False, encoding="utf-8-sig")
    # v1.1(#1)：NaN→None，否則json.dump會寫出非法的NaN字面值，
    # 瀏覽器/Node的JSON.parse與GAS等下游消費端會直接解析失敗
    out_json = out.astype(object).where(pd.notna(out), None)
    payload = {"updated": ts, "source": "US StockRadar v1.1",
               "count": len(out), "results": out_json.to_dict(orient="records")}
    with open(os.path.join(base, CFG["OUTPUT_JSON"]), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, default=str)
    return out


# ============================================================
# Demo 合成資料（沙盒/無網路時驗證指標邏輯用）
# ============================================================
def make_demo_data(ticker: str):
    seed = sum(ord(c) for c in ticker)   # v1.1：hash()有隨機化不可重現，改穩定seed(對齊chips)
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(end=datetime.now(), periods=250)
    n = len(idx)   # v1.1(#2)：end落在週末時pandas會回傳249個索引，以實際長度為準
    scenario = seed % 3

    base = 100.0
    px = [base]
    for i in range(1, n):
        if scenario == 0:   # 標準 N 字：漲→回檔→現價貼近C
            drift = 0.004 if i < 150 else (-0.0035 if i < 210 else 0.0005)
        elif scenario == 1: # 波段新高：一路漲
            drift = 0.0035
        else:               # 空頭：跌破起漲點
            drift = 0.003 if i < 80 else -0.004
        px.append(px[-1] * (1 + drift + rng.normal(0, 0.012)))
    close = np.array(px)
    high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    vol = rng.integers(2e6, 8e6, n).astype(float)
    vol[-5:] *= 0.7  # 模擬回檔量縮
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low,
                       "Close": close, "Volume": vol}, index=idx)
    info = {"revenueGrowth": 0.22, "earningsGrowth": 0.18, "grossMargins": 0.45,
            "shortPercentOfFloat": 0.025, "heldPercentInstitutions": 0.72}
    return df, info


if __name__ == "__main__":
    demo = "--demo" in sys.argv
    result = run_scan(demo=demo)
    cols = ["ticker", "name", "price", "sha_signal", "n_stage",
            "compositeScore", "tier"]
    cols = [c for c in cols if c in result.columns]
    print(result[cols].to_string(index=False))
    print(f"\n完成：{len(result)} 支 → {CFG['OUTPUT_CSV']} / {CFG['OUTPUT_JSON']}")
