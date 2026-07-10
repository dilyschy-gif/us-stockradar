from __future__ import annotations

import argparse
import csv
import html
import json
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus, urlparse

try:  # 讓 --no-news 與單元測試在未安裝網路套件時仍可執行
    import feedparser
except ImportError:  # pragma: no cover - 正式 workflow 會由 requirements.txt 安裝
    feedparser = None

try:
    import requests
except ImportError:  # pragma: no cover - 正式 workflow 會由 requirements.txt 安裝
    requests = None


ROOT = Path(__file__).resolve().parent
SCREENER_PATH = ROOT / "us-data.json"
CHIPS_PATH = ROOT / "chips_proxy.json"
UNIVERSE_PATH = ROOT / "universe.csv"
SITE_DIR = ROOT / "site"
DATA_DIR = SITE_DIR / "data"
HISTORY_DIR = DATA_DIR / "history"
INDEX_PATH = SITE_DIR / "index.html"
LATEST_PATH = DATA_DIR / "latest.json"

TWN_TZ = timezone(timedelta(hours=8))
RSS_ENDPOINT = "https://news.google.com/rss/search?q={query}+when:1d&hl=en-US&gl=US&ceid=US:en"
HTTP_HEADERS = {"User-Agent": "us-stockradar/1.0 (+https://github.com/dilyschy-gif/us-stockradar)"}

ENTRY_MIN = 60
HEAT_HOT = 65
HEAT_QUIET = 35
STALE_AFTER_HOURS = 96


LAYERS = {
    "buzz": {
        "label": "新聞／市場聲量",
        "subtitle": "市場正在集中討論哪些股票與題材",
        "queries": [
            "US stock market trending stocks",
            "Wall Street AI stocks",
            "semiconductor stocks",
            "data center power stocks",
            "cybersecurity stocks",
            "defense stocks",
            "GLP-1 stocks",
        ],
    },
    "research": {
        "label": "分析師／研究觀點",
        "subtitle": "升降評、目標價與產業研究的共同焦點",
        "queries": [
            "Wall Street analyst upgrade downgrade stocks",
            "analyst price target AI stocks",
            "semiconductor analyst outlook",
            "data center infrastructure analyst stocks",
            "cybersecurity analyst stocks",
        ],
    },
    "catalyst": {
        "label": "事件／資金催化",
        "subtitle": "財報、成交量、選擇權與內部人事件的新聞線索",
        "queries": [
            "US stocks earnings surprise",
            "US stocks unusual volume breakout",
            "unusual options activity stocks",
            "insider buying stocks Form 4",
            "SEC filing stock catalyst",
        ],
    },
}


TOPIC_KEYWORDS = {
    "AI晶片": ("AI CHIP", "GPU", "AI ACCELERATOR", "NVIDIA", "CUDA"),
    "ASIC／客製晶片": ("ASIC", "CUSTOM SILICON", "CUSTOM CHIP"),
    "半導體設備": ("SEMICONDUCTOR EQUIPMENT", "WAFER FAB EQUIPMENT", "CHIP EQUIPMENT"),
    "記憶體／儲存": ("MEMORY CHIP", "DRAM", "HBM", "NAND", "DATA STORAGE"),
    "雲端／AI軟體": ("CLOUD COMPUTING", "AI SOFTWARE", "GENERATIVE AI", "ENTERPRISE AI"),
    "AI網通": ("AI NETWORKING", "ETHERNET SWITCH", "DATA CENTER NETWORK"),
    "AI電力／散熱": ("DATA CENTER POWER", "AI POWER", "LIQUID COOLING", "DATA CENTER COOLING"),
    "核能／電力": ("NUCLEAR POWER", "URANIUM", "POWER GENERATION", "ELECTRICITY DEMAND"),
    "光通訊／連接器": ("OPTICAL NETWORK", "OPTICAL COMMUNICATION", "FIBER OPTIC", "CONNECTOR"),
    "國防／航太": ("DEFENSE STOCK", "AEROSPACE", "MISSILE", "MILITARY CONTRACT"),
    "金融": ("BANK STOCK", "FINANCIAL STOCK", "INTEREST MARGIN"),
    "GLP-1／生技": ("GLP-1", "OBESITY DRUG", "WEIGHT-LOSS DRUG", "BIOTECH"),
    "資安": ("CYBERSECURITY", "CYBER SECURITY", "RANSOMWARE", "ZERO TRUST"),
}

POSITIVE_TERMS = (
    "BEAT ESTIMATES", "RAISES GUIDANCE", "UPGRADE", "OUTPERFORM", "BUY RATING",
    "PRICE TARGET RAISED", "RECORD REVENUE", "BREAKOUT", "STRONG DEMAND", "SURGE",
)
NEGATIVE_TERMS = (
    "MISSES ESTIMATES", "CUTS GUIDANCE", "DOWNGRADE", "UNDERPERFORM", "SELL RATING",
    "PRICE TARGET CUT", "WEAK DEMAND", "INVESTIGATION", "PLUNGE", "LAYOFF",
)

# 只補充容易在新聞中出現、但與 universe.csv 正式名稱不同的常見公司名。
EXTRA_ALIASES = {
    "NVDA": ("NVIDIA",),
    "AVGO": ("BROADCOM",),
    "AMAT": ("APPLIED MATERIALS",),
    "LRCX": ("LAM RESEARCH",),
    "KLAC": ("KLA CORP", "KLA CORPORATION"),
    "WDC": ("WESTERN DIGITAL",),
    "MU": ("MICRON", "MICRON TECHNOLOGY"),
    "MSFT": ("MICROSOFT",),
    "ORCL": ("ORACLE",),
    "ANET": ("ARISTA", "ARISTA NETWORKS"),
    "VRT": ("VERTIV",),
    "ETN": ("EATON",),
    "CEG": ("CONSTELLATION ENERGY",),
    "VST": ("VISTRA",),
    "APH": ("AMPHENOL",),
    "TEL": ("TE CONNECTIVITY",),
    "LMT": ("LOCKHEED MARTIN",),
    "RTX": ("RTX CORP", "RAYTHEON"),
    "JPM": ("JPMORGAN", "JPMORGAN CHASE"),
    "LLY": ("ELI LILLY", "LILLY"),
    "CRWD": ("CROWDSTRIKE",),
    "PANW": ("PALO ALTO NETWORKS",),
    "COHR": ("COHERENT",),
    "CSCO": ("CISCO",),
    "HPE": ("HEWLETT PACKARD ENTERPRISE",),
    "GLW": ("CORNING",),
    "BE": ("BLOOM ENERGY",),
    "P": ("EVERPURE",),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="產生美股市場熱度雷達靜態網站")
    parser.add_argument("--no-news", action="store_true", help="跳過 RSS；用於離線測試與除錯")
    args = parser.parse_args()

    dashboard = build_dashboard(fetch_news=not args.no_news)
    write_outputs(dashboard)
    print(dashboard["headline"])
    print(f"Generated {INDEX_PATH} / {LATEST_PATH}")


def build_dashboard(fetch_news: bool = True) -> dict:
    screener = load_json(SCREENER_PATH, {"results": []})
    chips = load_json(CHIPS_PATH, {"results": []})
    universe = load_universe(UNIVERSE_PATH)
    screener_rows = [row for row in screener.get("results", []) if isinstance(row, dict)]
    stocks = merge_stock_metadata(universe, screener_rows)

    items = collect_items() if fetch_news else []
    for item in items:
        enrich_item(item, stocks)

    radar_rows = build_radar_rows(screener_rows, items, chips)
    quality = build_data_quality(screener, screener_rows, items, fetch_news)
    layers = summarize_layers(items)
    sectors = build_sector_heat(radar_rows)
    topics = rank_topics(items)

    priority = [row for row in radar_rows if row["signal_code"] == "priority"][:5]
    quiet = [row for row in radar_rows if row["signal_code"] == "quiet"][:5]
    hot_risk = [row for row in radar_rows if row["signal_code"] == "hot_risk"][:5]
    watchlist = priority or [row for row in radar_rows if row["eligible"]][:5]
    headline = build_headline(priority, quiet, hot_risk, topics)

    notes = [
        "新聞熱度來自 Google News 公開 RSS，代表新聞／市場聲量，不等於 Reddit、Stocktwits 或實際社群貼文數。",
        "進場分數沿用 US StockRadar 的技術、基本面、籌碼替代與量能評分；排除層為風險否決，即使總分高也不列為候選。",
        "13F 與機構持股資料具有申報落後性，只作中期背景驗證，不視為當日法人買賣超。",
        "熱度高不等於適合追價；本頁刻意分開顯示市場熱度與進場準備度。重要標的仍需人工確認 K 線、財報與事件來源。",
    ]

    now = datetime.now(TWN_TZ)
    return {
        "generated_at": now.isoformat(),
        "source_updated": screener.get("updated"),
        "source_market_date": screener.get("market_date"),
        "data_quality": quality,
        "headline": headline,
        "layers": layers,
        "topics": topics[:12],
        "sectors": sectors,
        "priority": priority,
        "quiet_candidates": quiet,
        "hot_risk": hot_risk,
        "watchlist": watchlist,
        "radar_rows": radar_rows,
        "source_items": serialize_items(items[:120]),
        "notes": notes,
        "methodology": {
            "entry_score": "US StockRadar compositeScore（技術40＋基本面30＋籌碼替代15＋量能15）",
            "buzz_score": "近24小時新聞提及、時效與來源多樣性標準化為0–100",
            "priority_rule": f"非排除層且進場分≥{ENTRY_MIN}、熱度分≥{HEAT_HOT}",
        },
    }


def load_json(path: Path, default: dict) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else default
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Load failed for {path}: {exc}")
        return default


def load_universe(path: Path) -> list[dict]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = []
            for row in csv.DictReader(handle):
                ticker = clean(row.get("ticker", "")).upper()
                if not ticker:
                    continue
                rows.append({
                    "ticker": ticker,
                    "name": clean(row.get("name", "")) or ticker,
                    "sector": clean(row.get("sector", "")) or "未分類",
                    "theme": clean(row.get("theme", "")) or "未分類",
                })
            return rows
    except OSError as exc:
        print(f"Load failed for {path}: {exc}")
        return []


def merge_stock_metadata(universe: list[dict], screener_rows: list[dict]) -> list[dict]:
    by_ticker = {row["ticker"]: dict(row) for row in universe}
    for row in screener_rows:
        ticker = clean(row.get("ticker", "")).upper()
        if not ticker:
            continue
        base = by_ticker.setdefault(ticker, {"ticker": ticker, "name": ticker, "sector": "未分類", "theme": "未分類"})
        for key in ("name", "sector", "theme"):
            if clean(row.get(key, "")):
                base[key] = clean(row[key])
    return list(by_ticker.values())


def collect_items() -> list[dict]:
    if requests is None or feedparser is None:
        print("News dependencies are unavailable; continuing with screener data only.")
        return []

    session = requests.Session()
    rows = []
    for layer, config in LAYERS.items():
        seen: set[str] = set()
        for query in config["queries"]:
            response = fetch_rss(session, RSS_ENDPOINT.format(query=quote_plus(query)))
            if response is None:
                continue
            feed = feedparser.parse(response.content)
            for entry in feed.entries[:12]:
                title = clean(getattr(entry, "title", ""))
                summary = clean(getattr(entry, "summary", ""))
                url = clean(getattr(entry, "link", ""))
                key = url or title
                if not title or key in seen:
                    continue
                seen.add(key)
                rows.append({
                    "layer": layer,
                    "query": query,
                    "source": source_name(entry, title),
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "published_at": parse_rss_time(getattr(entry, "published", None)),
                })
    return rows


def fetch_rss(session, url: str):
    for attempt in range(2):
        try:
            response = session.get(url, headers=HTTP_HEADERS, timeout=18)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            if attempt == 1:
                print(f"Fetch failed for {url}: {exc}")
                return None
            time.sleep(0.6)
    return None


def enrich_item(item: dict, stocks: list[dict]) -> None:
    text = f"{item.get('title', '')} {item.get('summary', '')}".upper()
    matched = [stock for stock in stocks if stock_matches(text, stock)]
    item["stocks"] = [
        {"ticker": stock["ticker"], "name": stock["name"], "sector": stock["sector"], "theme": stock["theme"]}
        for stock in matched
    ]

    topics = [topic for topic, aliases in TOPIC_KEYWORDS.items() if any(contains_phrase(text, alias) for alias in aliases)]
    for stock in matched:
        for value in (stock.get("theme"), stock.get("sector")):
            if value and value != "未分類" and value not in topics:
                topics.append(value)
    item["topics"] = topics

    score = sum(text.count(term) for term in POSITIVE_TERMS) - sum(text.count(term) for term in NEGATIVE_TERMS)
    item["sentiment_score"] = score
    item["sentiment"] = "偏樂觀" if score >= 2 else "偏保守" if score <= -2 else "中性"


def stock_matches(text: str, stock: dict) -> bool:
    ticker = clean(stock.get("ticker", "")).upper()
    if not ticker:
        return False

    # $NVDA / NASDAQ:NVDA 可安全辨識；1–2字母裸代號（P、BE、MU）不直接比對，
    # 避免把英文單字或單一字母誤認成股票。
    escaped = re.escape(ticker)
    qualified_pattern = r"(?:\$" + escaped + rf"|(?:NASDAQ|NYSE|AMEX)\s*:\s*{escaped})(?![A-Z0-9])"
    if re.search(qualified_pattern, text):
        return True
    if len(ticker) >= 3 and re.search(rf"(?<![A-Z0-9]){escaped}(?![A-Z0-9])", text):
        return True

    aliases = list(EXTRA_ALIASES.get(ticker, ()))
    name = clean(stock.get("name", "")).upper()
    if name and name != ticker:
        aliases.append(name)
    return any(contains_phrase(text, alias) for alias in aliases if len(alias) >= 4)


def contains_phrase(text: str, phrase: str) -> bool:
    phrase = clean(phrase).upper()
    if not phrase:
        return False
    return bool(re.search(rf"(?<![A-Z0-9]){re.escape(phrase)}(?![A-Z0-9])", text))


def summarize_layers(items: list[dict]) -> dict:
    result = {}
    for key, config in LAYERS.items():
        layer_items = [item for item in items if item["layer"] == key]
        matched = [item for item in layer_items if item.get("stocks") or item.get("topics")]
        result[key] = {
            "label": config["label"],
            "subtitle": config["subtitle"],
            "raw_count": len(layer_items),
            "signal_count": len(matched),
            "top_stocks": rank_news_stocks(matched)[:8],
            "top_topics": rank_topics(matched)[:8],
        }
    return result


def rank_news_stocks(items: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc)
    bucket: dict[str, dict] = {}
    seen_mentions: set[tuple[str, str]] = set()
    for item in items:
        heat = article_heat(item, now)
        for stock in item.get("stocks", []):
            ticker = stock["ticker"]
            article_key = item.get("url") or item.get("title") or str(id(item))
            mention_key = (ticker, article_key)
            if mention_key in seen_mentions:
                continue
            seen_mentions.add(mention_key)
            row = bucket.setdefault(ticker, {
                "ticker": ticker, "name": stock["name"], "mentions": 0,
                "heat_raw": 0.0, "sources": set(), "sentiment_sum": 0,
                "layer_mentions": defaultdict(int),
            })
            row["mentions"] += 1
            row["heat_raw"] += heat
            row["sources"].add(item["source"])
            row["sentiment_sum"] += item["sentiment_score"]
            row["layer_mentions"][item["layer"]] += 1

    rows = []
    for row in bucket.values():
        raw = row["heat_raw"] + row["mentions"] * 1.5 + len(row["sources"]) * 1.2
        rows.append({
            "ticker": row["ticker"], "name": row["name"], "mentions": row["mentions"],
            "sources": len(row["sources"]), "heat_raw": round(raw, 2),
            "sentiment_sum": row["sentiment_sum"],
            "layer_mentions": dict(row["layer_mentions"]),
        })
    rows.sort(key=lambda row: (row["heat_raw"], row["mentions"]), reverse=True)
    max_heat = max([row["heat_raw"] for row in rows] or [1])
    for row in rows:
        row["buzz_score"] = round(row["heat_raw"] / max_heat * 100)
    return rows


def rank_topics(items: list[dict]) -> list[dict]:
    bucket: dict[str, dict] = {}
    seen_mentions: set[tuple[str, str]] = set()
    now = datetime.now(timezone.utc)
    for item in items:
        for topic in item.get("topics", []):
            article_key = item.get("url") or item.get("title") or str(id(item))
            mention_key = (topic, article_key)
            if mention_key in seen_mentions:
                continue
            seen_mentions.add(mention_key)
            row = bucket.setdefault(topic, {"name": topic, "mentions": 0, "heat": 0.0, "sources": set()})
            row["mentions"] += 1
            row["heat"] += article_heat(item, now)
            row["sources"].add(item["source"])
    rows = [
        {"name": row["name"], "mentions": row["mentions"], "sources": len(row["sources"]),
         "heat": round(row["heat"] + row["mentions"] * 1.2, 1)}
        for row in bucket.values()
    ]
    return sorted(rows, key=lambda row: (row["heat"], row["mentions"]), reverse=True)


def build_radar_rows(screener_rows: list[dict], items: list[dict], chips_payload: dict) -> list[dict]:
    news_by_ticker = {row["ticker"]: row for row in rank_news_stocks(items)}
    chips_by_ticker = {
        clean(row.get("ticker", "")).upper(): row
        for row in chips_payload.get("results", []) if isinstance(row, dict)
    }
    rows = []
    for source in screener_rows:
        ticker = clean(source.get("ticker", "")).upper()
        if not ticker:
            continue
        news = news_by_ticker.get(ticker, {})
        chip = chips_by_ticker.get(ticker, {})
        entry_score = to_number(source.get("compositeScore")) or 0
        buzz_score = to_number(news.get("buzz_score")) or 0
        tier = clean(source.get("tier", "")) or "未分層"
        status = clean(source.get("status", ""))
        eligible = status == "OK" and not tier.startswith("排除")
        signal_code, signal = judge_signal(entry_score, buzz_score, eligible, tier)
        rows.append({
            **source,
            "ticker": ticker,
            "entry_score": round(entry_score),
            "buzz_score": round(buzz_score),
            "buzz_mentions": int(news.get("mentions", 0) or 0),
            "buzz_sources": int(news.get("sources", 0) or 0),
            "eligible": eligible,
            "signal_code": signal_code,
            "signal": signal,
            "instTrendScore": chip.get("instTrendScore"),
            "instTrendDetail": chip.get("instTrendDetail") or "未更新／無資料",
            "sec_edgar_link": chip.get("sec_edgar_link") or "",
        })

    rank = {"priority": 0, "quiet": 1, "confirm": 2, "hot_risk": 3, "watch": 4}
    rows.sort(key=lambda row: (
        rank.get(row["signal_code"], 9),
        -int(row["eligible"]),
        -row["entry_score"],
        -row["buzz_score"],
    ))
    return rows


def judge_signal(entry_score: float, buzz_score: float, eligible: bool, tier: str) -> tuple[str, str]:
    if buzz_score >= HEAT_HOT and not eligible:
        if "波段新高" in tier:
            return "hot_risk", "市場很熱但位階偏高：避免追價"
        return "hot_risk", "市場很熱但技術風險否決：先不追"
    if eligible and entry_score >= ENTRY_MIN and buzz_score >= HEAT_HOT:
        return "priority", "熱度＋結構共振：優先研究"
    if eligible and entry_score >= ENTRY_MIN and buzz_score < HEAT_QUIET:
        return "quiet", "結構較佳但新聞低熱：早期候選"
    if eligible and entry_score >= ENTRY_MIN:
        return "confirm", "結構較佳：等待熱度或價量確認"
    if not eligible:
        return "watch", "目前屬排除層：保留觀察"
    return "watch", "條件尚未共振：持續追蹤"


def build_sector_heat(rows: list[dict]) -> list[dict]:
    bucket = defaultdict(lambda: {"stocks": 0, "eligible": 0, "entry_sum": 0.0, "buzz_sum": 0.0, "top": None})
    for row in rows:
        sector = clean(row.get("sector", "")) or "未分類"
        slot = bucket[sector]
        slot["stocks"] += 1
        slot["eligible"] += int(row["eligible"])
        slot["entry_sum"] += row["entry_score"]
        slot["buzz_sum"] += row["buzz_score"]
        if slot["top"] is None or row["entry_score"] > slot["top"]["entry_score"]:
            slot["top"] = row

    max_buzz = max([value["buzz_sum"] for value in bucket.values()] or [1])
    result = []
    for sector, value in bucket.items():
        avg_entry = value["entry_sum"] / value["stocks"] if value["stocks"] else 0
        buzz_relative = value["buzz_sum"] / max_buzz * 100 if max_buzz else 0
        result.append({
            "sector": sector,
            "stock_count": value["stocks"],
            "eligible_count": value["eligible"],
            "avg_entry_score": round(avg_entry),
            "news_heat": round(buzz_relative),
            "sector_score": round(avg_entry * 0.55 + buzz_relative * 0.45),
            "top_ticker": value["top"]["ticker"] if value["top"] else "－",
        })
    return sorted(result, key=lambda row: (row["sector_score"], row["eligible_count"]), reverse=True)


def build_data_quality(screener: dict, rows: list[dict], items: list[dict], fetch_news: bool) -> dict:
    ok_count = sum(clean(row.get("status", "")) == "OK" for row in rows)
    error_count = len(rows) - ok_count
    updated = parse_screener_time(screener.get("updated"))
    age_hours = None
    if updated:
        age_hours = max((datetime.now(TWN_TZ) - updated).total_seconds() / 3600, 0)
    stale = age_hours is None or age_hours > STALE_AFTER_HOURS
    news_state = "live" if items else ("skipped" if not fetch_news else "unavailable")
    if rows and items and not stale:
        state = "live"
    elif rows:
        state = "market_only"
    elif items:
        state = "news_only"
    else:
        state = "empty"
    return {
        "state": state,
        "screener_rows": len(rows),
        "screener_ok": ok_count,
        "screener_errors": error_count,
        "screener_updated": screener.get("updated"),
        "market_date": screener.get("market_date"),
        "screener_age_hours": round(age_hours, 1) if age_hours is not None else None,
        "screener_stale": stale,
        "news_state": news_state,
        "news_items": len(items),
        "summary": quality_summary(state, stale, error_count, news_state),
    }


def quality_summary(state: str, stale: bool, errors: int, news_state: str) -> str:
    parts = []
    if state == "empty":
        return "選股與新聞資料都不可用，本次頁面不得作為判讀依據。"
    if stale:
        parts.append("選股資料可能過期")
    if errors:
        parts.append(f"{errors}檔選股資料錯誤")
    if news_state == "unavailable":
        parts.append("新聞RSS抓取失敗，熱度欄暫缺")
    if news_state == "skipped":
        parts.append("離線模式：未抓新聞")
    return "；".join(parts) + "。" if parts else "選股與新聞資料已成功整合。"


def build_headline(priority: list[dict], quiet: list[dict], hot_risk: list[dict], topics: list[dict]) -> str:
    topic_text = "、".join(row["name"] for row in topics[:3]) or "市場題材尚未形成明顯共識"
    if priority:
        names = "、".join(row["ticker"] for row in priority[:5])
        return f"今日美股主要焦點：{topic_text}；熱度與進場結構同時共振的優先研究標的是 {names}。"
    if quiet:
        names = "、".join(row["ticker"] for row in quiet[:5])
        suffix = "；另有高熱但被風險否決標的，請勿直接追價。" if hot_risk else "。"
        return f"今日美股主要焦點：{topic_text}；目前沒有熱度與結構完全共振標的，低熱但結構較佳的早期候選為 {names}{suffix}"
    return f"今日美股主要焦點：{topic_text}；目前沒有符合進場門檻的共振候選，先維持觀察。"


def write_outputs(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    LATEST_PATH.write_text(payload, encoding="utf-8")
    INDEX_PATH.write_text(render_html(data), encoding="utf-8")
    day = str(data.get("generated_at", ""))[:10] or datetime.now(TWN_TZ).strftime("%Y-%m-%d")
    (HISTORY_DIR / f"{day}.json").write_text(payload, encoding="utf-8")
    (SITE_DIR / ".nojekyll").touch()


def render_html(data: dict) -> str:
    quality = data["data_quality"]
    banners = []
    if quality["state"] != "live" or quality["screener_stale"] or quality["screener_errors"]:
        banners.append(f"<div class='banner warning'>{esc(quality['summary'])}</div>")
    else:
        banners.append(f"<div class='banner ok'>{esc(quality['summary'])}</div>")

    generated = format_time(data["generated_at"])
    source_updated = esc(data.get("source_updated") or "未知")
    source_market_date = esc(data.get("source_market_date") or "目前JSON尚未提供；下次掃描後補上")
    eligible_count = sum(row["eligible"] for row in data["radar_rows"])
    all_table = render_stock_table(data["radar_rows"], limit=40, compact=False)
    priority_table = render_stock_table(data["priority"] or data["watchlist"], limit=5, compact=True)
    quiet_table = render_stock_table(data["quiet_candidates"], limit=5, compact=True)
    risk_table = render_stock_table(data["hot_risk"], limit=5, compact=True)
    sector_table = render_sector_table(data["sectors"])
    layer_cards = render_layer_cards(data["layers"])
    topics = "".join(f"<span class='tag'>{esc(row['name'])} · {row['mentions']}</span>" for row in data["topics"][:10]) or "<span class='muted'>目前沒有新聞題材資料</span>"
    sources = render_sources(data["source_items"])
    notes = "".join(f"<li>{esc(note)}</li>" for note in data["notes"])

    return f"""<!doctype html>
<html lang='zh-Hant'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width,initial-scale=1'>
  <title>美股市場熱度雷達｜US StockRadar</title>
  <style>
    :root{{--ink:#162033;--muted:#667085;--line:#e4e7ec;--panel:#fff;--navy:#173b57;--blue:#1f6feb;--teal:#0f766e;--amber:#b54708;--red:#b42318;--bg:#f5f7fb}}
    *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);font-family:'Microsoft JhengHei','Noto Sans TC',Arial,sans-serif}}
    a{{color:var(--blue);text-decoration:none}} .wrap{{max-width:1320px;margin:auto;padding:26px 20px 46px}}
    .top{{display:flex;justify-content:space-between;align-items:end;gap:18px;margin-bottom:16px}} h1{{margin:0;font-size:30px;letter-spacing:.02em}} h2{{font-size:19px;margin:0 0 12px}}
    .subtitle,.muted{{color:var(--muted);font-size:13px}} .nav{{display:flex;gap:8px;margin:14px 0 20px}} .nav a{{border:1px solid var(--line);background:white;border-radius:999px;padding:8px 14px;color:var(--ink)}} .nav a.active{{background:var(--navy);color:#fff;border-color:var(--navy)}}
    .banner{{border-radius:10px;padding:12px 15px;margin:0 0 14px;font-weight:650}} .banner.ok{{background:#ecfdf3;border:1px solid #abefc6;color:#067647}} .banner.warning{{background:#fffaeb;border:1px solid #fedf89;color:#93370d}}
    .headline{{background:linear-gradient(120deg,#173b57,#245d79);color:#fff;border-radius:14px;padding:19px 21px;line-height:1.75;margin-bottom:16px;box-shadow:0 8px 20px rgba(23,59,87,.12)}}
    .metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}} .metric,.panel{{background:var(--panel);border:1px solid var(--line);border-radius:12px;box-shadow:0 1px 2px rgba(16,24,40,.03)}} .metric{{padding:14px 15px}} .metric .value{{font-size:25px;font-weight:750;margin-top:6px}}
    .grid2{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-bottom:14px}} .grid3{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-bottom:14px}} .panel{{padding:16px;overflow:hidden;margin-bottom:14px}}
    .layer .count{{font-size:27px;font-weight:750;color:var(--navy);margin:8px 0}} .tag{{display:inline-block;border:1px solid #b2ddff;background:#eff8ff;color:#175cd3;border-radius:999px;padding:5px 9px;margin:0 6px 6px 0;font-size:12px}}
    .table-scroll{{overflow:auto;border:1px solid var(--line);border-radius:10px;max-height:590px}} table{{width:100%;border-collapse:collapse;background:#fff;font-size:13px}} th,td{{padding:10px 9px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}} th{{position:sticky;top:0;background:#f9fafb;color:#475467;z-index:1}} td.wrap-text{{white-space:normal;min-width:210px;line-height:1.5}}
    .pill{{display:inline-block;padding:4px 7px;border-radius:999px;font-size:12px;font-weight:650}} .pill.priority{{background:#dcfae6;color:#067647}} .pill.quiet{{background:#eff8ff;color:#175cd3}} .pill.hot_risk{{background:#fee4e2;color:#b42318}} .pill.confirm{{background:#fef0c7;color:#b54708}} .pill.watch{{background:#f2f4f7;color:#475467}}
    .score{{font-weight:750}} .hot{{color:#b42318}} .entry{{color:#175cd3}} .footnote{{font-size:12px;color:var(--muted);line-height:1.7}}
    @media(max-width:980px){{.metrics,.grid2,.grid3{{grid-template-columns:1fr 1fr}}.top{{display:block}}}} @media(max-width:640px){{.metrics,.grid2,.grid3{{grid-template-columns:1fr}}.wrap{{padding:18px 12px 36px}}h1{{font-size:25px}}}}
  </style>
</head>
<body><main class='wrap'>
  <div class='top'><div><h1>美股市場熱度雷達</h1><div class='subtitle'>US StockRadar · 熱度與進場準備度分開判讀</div></div><div class='muted'>頁面產生：{esc(generated)}<br>行情日期：{source_market_date}<br>選股資料：{source_updated}</div></div>
  <nav class='nav'><a href='https://dilyschy-gif.github.io/market-radar/'>台股雷達</a><a class='active' href='https://dilyschy-gif.github.io/us-stockradar/'>美股雷達</a></nav>
  {''.join(banners)}
  <section class='headline'>{esc(data['headline'])}</section>
  <section class='metrics'>
    <div class='metric'><div class='muted'>美股母池</div><div class='value'>{len(data['radar_rows'])}</div></div>
    <div class='metric'><div class='muted'>非排除層</div><div class='value'>{eligible_count}</div></div>
    <div class='metric'><div class='muted'>新聞原始訊號</div><div class='value'>{quality['news_items']}</div></div>
    <div class='metric'><div class='muted'>優先共振候選</div><div class='value'>{len(data['priority'])}</div></div>
  </section>
  <section class='grid3'>{layer_cards}</section>
  <section class='panel'><h2>今日題材共識</h2>{topics}</section>
  <section class='grid2'>
    <div class='panel'><h2>優先研究：熱度 × 結構共振</h2>{priority_table}</div>
    <div class='panel'><h2>低熱早期候選：結構佳、尚未擁擠</h2>{quiet_table}</div>
  </section>
  <section class='panel'><h2>高熱風險區：市場在追、系統暫不追</h2>{risk_table}</section>
  <section class='panel'><h2>產業熱度與進場準備度</h2>{sector_table}</section>
  <section class='panel'><h2>完整美股雷達</h2>{all_table}</section>
  <section class='panel'><h2>新聞來源明細</h2>{sources}</section>
  <section class='panel footnote'><h2 style='font-size:15px'>資料與方法限制</h2><ul>{notes}</ul></section>
</main></body></html>"""


def render_layer_cards(layers: dict) -> str:
    cards = []
    for key in ("buzz", "research", "catalyst"):
        layer = layers[key]
        stocks = "、".join(row["ticker"] for row in layer["top_stocks"][:5]) or "尚無"
        topics = "、".join(row["name"] for row in layer["top_topics"][:4]) or "尚無"
        cards.append(
            f"<div class='panel layer'><h2>{esc(layer['label'])}</h2><div class='muted'>{esc(layer['subtitle'])}</div>"
            f"<div class='count'>{layer['signal_count']}</div><div class='muted'>有效訊號／原始 {layer['raw_count']}</div>"
            f"<p><b>股票：</b>{esc(stocks)}</p><p><b>題材：</b>{esc(topics)}</p></div>"
        )
    return "".join(cards)


def render_stock_table(rows: list[dict], limit: int, compact: bool) -> str:
    if not rows:
        return "<p class='muted'>目前沒有符合條件的標的。</p>"
    headers = ["股票", "價格", "進場分", "熱度分", "層級", "N字階段", "判讀"]
    if not compact:
        headers.insert(6, "主動機構背景")
    head = "".join(f"<th>{esc(value)}</th>" for value in headers)
    body = []
    for row in rows[:limit]:
        ticker = esc(row.get("ticker", ""))
        cells = [
            f"<td><a href='https://finance.yahoo.com/quote/{ticker}' target='_blank' rel='noreferrer'><b>{ticker}</b></a><br><span class='muted'>{esc(row.get('name',''))}</span></td>",
            f"<td>{esc(row.get('price','－'))}</td>",
            f"<td class='score entry'>{row.get('entry_score',0)}</td>",
            f"<td class='score hot'>{row.get('buzz_score',0)}</td>",
            f"<td>{esc(row.get('tier',''))}</td>",
            f"<td>{esc(row.get('n_stage',''))}</td>",
        ]
        if not compact:
            cells.append(f"<td class='wrap-text'>{esc(row.get('instTrendDetail',''))}</td>")
        cells.append(f"<td class='wrap-text'><span class='pill {esc(row.get('signal_code','watch'))}'>{esc(row.get('signal',''))}</span></td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f"<div class='table-scroll'><table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def render_sector_table(rows: list[dict]) -> str:
    if not rows:
        return "<p class='muted'>目前沒有產業資料。</p>"
    header = "".join(f"<th>{value}</th>" for value in ("產業", "綜合熱度", "新聞熱度", "平均進場分", "非排除／母池", "代表股"))
    body = "".join(
        f"<tr><td>{esc(row['sector'])}</td><td class='score'>{row['sector_score']}</td><td>{row['news_heat']}</td>"
        f"<td>{row['avg_entry_score']}</td><td>{row['eligible_count']}／{row['stock_count']}</td><td>{esc(row['top_ticker'])}</td></tr>"
        for row in rows
    )
    return f"<div class='table-scroll'><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>"


def render_sources(items: list[dict]) -> str:
    if not items:
        return "<p class='muted'>本次沒有取得新聞RSS；頁面仍保留選股結果，但熱度欄為0。</p>"
    header = "".join(f"<th>{value}</th>" for value in ("時間", "層級", "來源", "標題", "股票", "題材"))
    body = []
    for item in items[:80]:
        url = safe_url(item.get("url", ""))
        title = esc(item.get("title", ""))
        title_html = f"<a href='{esc(url)}' target='_blank' rel='noreferrer'>{title}</a>" if url else title
        tickers = "、".join(stock["ticker"] for stock in item.get("stocks", []))
        topics = "、".join(item.get("topics", []))
        body.append(
            f"<tr><td>{esc(format_time(item.get('published_at','')))}</td><td>{esc(LAYERS[item['layer']]['label'])}</td>"
            f"<td>{esc(item.get('source',''))}</td><td class='wrap-text'>{title_html}</td><td>{esc(tickers)}</td><td class='wrap-text'>{esc(topics)}</td></tr>"
        )
    return f"<div class='table-scroll'><table><thead><tr>{header}</tr></thead><tbody>{''.join(body)}</tbody></table></div>"


def serialize_items(items: list[dict]) -> list[dict]:
    result = []
    for item in items:
        row = dict(item)
        if isinstance(row.get("published_at"), datetime):
            row["published_at"] = row["published_at"].isoformat()
        result.append(row)
    return result


def article_heat(item: dict, now: datetime) -> float:
    published = item.get("published_at")
    if not isinstance(published, datetime):
        published = now
    age_hours = max((now - published.astimezone(timezone.utc)).total_seconds() / 3600, 0)
    return 3 + max(0.2, 2.0 - age_hours / 18) + abs(item.get("sentiment_score", 0)) * 0.35


def parse_rss_time(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc)
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def parse_screener_time(value: object) -> datetime | None:
    if not value:
        return None
    text = clean(str(value))
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TWN_TZ)
            return dt.astimezone(TWN_TZ)
        except ValueError:
            continue
    return None


def source_name(entry: object, title: str) -> str:
    source = getattr(entry, "source", None)
    if isinstance(source, dict) and source.get("title"):
        return clean(source["title"])
    return clean(title.rsplit(" - ", 1)[-1]) if " - " in title else "Google News"


def format_time(value: str | datetime) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return "－"
    if value.tzinfo is None:
        value = value.replace(tzinfo=TWN_TZ)
    return value.astimezone(TWN_TZ).strftime("%Y-%m-%d %H:%M")


def safe_url(value: object) -> str:
    text = clean(str(value or ""))
    parsed = urlparse(text)
    return text if parsed.scheme in ("http", "https") else ""


def to_number(value: object) -> float | None:
    try:
        return float(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def clean(value: object) -> str:
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(value))
    return html.unescape(" ".join(text.split()))


def esc(value: object) -> str:
    return html.escape(str(value) if value is not None else "", quote=True)


if __name__ == "__main__":
    main()
