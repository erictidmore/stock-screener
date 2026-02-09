"""
filters.py — Warrant, China, and News Filters
All filtering logic in one module. Imports config for credentials and paths.
"""

import json
import re
import time
import urllib.request
from datetime import datetime, timedelta

import pytz

import config as cfg


# ============================================================================
# Warrant / unit / rights filter
# ============================================================================
WARRANT_RE = re.compile(r'[.\-]?(WS?|WT|PR|U|R)$', re.IGNORECASE)


# ============================================================================
# China stock filter (SEC EDGAR)
# ============================================================================
def _load_china_cache() -> dict:
    if cfg.CHINA_CACHE_FILE.exists():
        try:
            return json.loads(cfg.CHINA_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_china_cache(cache: dict):
    cfg.CHINA_CACHE_FILE.write_text(json.dumps(cache, indent=2))


def _get_sec_ticker_map() -> dict:
    """Download SEC ticker -> CIK mapping (cached in memory per run)."""
    url = "https://www.sec.gov/files/company_tickers.json"
    req = urllib.request.Request(url, headers={"User-Agent": cfg.SEC_USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return {entry["ticker"].upper(): entry["cik_str"] for entry in data.values()}


def _check_china_stock(cik: int) -> tuple:
    """Query SEC EDGAR for a company's domicile. Returns (is_china, country_code, inc_code, name)."""
    cik_padded = str(cik).zfill(10)
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    req = urllib.request.Request(url, headers={"User-Agent": cfg.SEC_USER_AGENT})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    biz = data.get("addresses", {}).get("business", {})
    country_code = biz.get("stateOrCountry", "") or ""
    inc_code = data.get("stateOfIncorporation", "") or ""
    name = data.get("name", "") or ""

    # Require both business address AND incorporation to be suspicious
    china_biz = country_code in cfg.CHINA_COUNTRY_CODES
    china_inc = inc_code in cfg.CHINA_COUNTRY_CODES
    is_china = china_biz and china_inc
    return is_china, country_code, inc_code, name


def filter_china_stocks(gainers: list) -> list:
    """Remove Chinese-domiciled stocks using SEC EDGAR data. Results are cached."""
    cache = _load_china_cache()

    symbols_to_check = [g["symbol"] for g in gainers if g["symbol"] not in cache]

    if symbols_to_check:
        print("  Checking SEC EDGAR for Chinese stocks...")
        try:
            ticker_map = _get_sec_ticker_map()
        except Exception as e:
            print(f"  WARNING: Could not fetch SEC ticker map: {e}")
            print("  Skipping China filter.")
            return gainers

        for sym in symbols_to_check:
            cik = ticker_map.get(sym)
            if cik is None:
                cache[sym] = {"is_china": False, "country": "??", "inc": "??",
                              "name": sym, "note": "Not in SEC"}
                print(f"    {sym:>6} -> not found in SEC (keeping)")
                continue
            try:
                is_china, country, inc, name = _check_china_stock(cik)
                cache[sym] = {"is_china": is_china, "country": country,
                              "inc": inc, "name": name}
                if is_china:
                    print(f"    {sym:>6} -> CHINA/SHELL ({country}/{inc}) — {name}")
                else:
                    print(f"    {sym:>6} -> OK ({country}/{inc})")
            except Exception as e:
                cache[sym] = {"is_china": False, "country": "??", "inc": "??",
                              "name": sym, "note": f"Lookup failed: {e}"}
                print(f"    {sym:>6} -> lookup failed ({e}), keeping")
            time.sleep(0.15)  # SEC rate limit ~10/sec

        _save_china_cache(cache)

    filtered = []
    removed = []
    for g in gainers:
        entry = cache.get(g["symbol"], {})
        if entry.get("is_china", False):
            removed.append(g["symbol"])
        else:
            filtered.append(g)

    if removed:
        print(f"  Removed {len(removed)} Chinese/shell stocks: {', '.join(removed)}")
    else:
        print("  No Chinese stocks detected.")

    return filtered


# ============================================================================
# News catalyst checker (Alpaca News API)
# ============================================================================
_ROUNDUP_RE = re.compile(
    r'stocks?\s+moving|pre-market session|after-market session|intraday session'
    r'|most active|biggest movers|top gainers|top losers'
    r'|mid-day gainers|mid-day losers|weekly gainer'
    r'|unusual volume|penny stocks?|meme stocks?',
    re.IGNORECASE,
)


def check_news_catalysts(gainers: list, hours: int = 48) -> list:
    """Check Alpaca news for each symbol. Adds 'news_catalyst' and 'news_headlines' to each entry."""
    from alpaca.data.historical.news import NewsClient
    from alpaca.data.requests import NewsRequest

    et = pytz.timezone("America/New_York")
    now = datetime.now(et)
    start = now - timedelta(hours=hours)

    client = NewsClient(cfg.APCA_API_KEY_ID, cfg.APCA_API_SECRET_KEY)

    print(f"  Checking news catalysts (last {hours}h)...")

    for g in gainers:
        sym = g["symbol"]
        try:
            req = NewsRequest(symbols=sym, start=start, limit=10,
                              include_content=False, exclude_contentless=False)
            news_set = client.get_news(request_params=req)
            articles = news_set.data.get("news", [])
        except Exception as e:
            print(f"    {sym:>6} -> news lookup failed ({e})")
            g["news_catalyst"] = None
            g["news_headlines"] = []
            continue

        # Filter out generic roundup headlines
        real_headlines = []
        for a in articles:
            if not _ROUNDUP_RE.search(a.headline):
                real_headlines.append({
                    "headline": a.headline,
                    "source": a.source,
                    "time": a.created_at.astimezone(et).strftime("%b %d %I:%M %p"),
                })

        has_catalyst = len(real_headlines) > 0
        g["news_catalyst"] = has_catalyst
        g["news_headlines"] = real_headlines

        tag = "CATALYST" if has_catalyst else "NO NEWS"
        top_hl = real_headlines[0]["headline"][:60] if real_headlines else "(no company-specific news)"
        print(f"    {sym:>6} -> {tag:<9} {top_hl}")
        time.sleep(0.1)

    return gainers


def filter_no_news(gainers: list) -> list:
    """Remove stocks with no news catalyst."""
    with_news = [g for g in gainers if g.get("news_catalyst", False)]
    removed = [g["symbol"] for g in gainers if not g.get("news_catalyst", False)]
    if removed:
        print(f"  Removed {len(removed)} stocks with no catalyst: {', '.join(removed)}")
    return with_news


def print_news_detail(gainers: list):
    """Show detailed news for stocks that passed the filter."""
    for g in gainers:
        headlines = g.get("news_headlines", [])
        if not headlines:
            continue
        print(f"    {g['symbol']}:")
        for h in headlines[:3]:
            print(f"      [{h['time']}] {h['source']} — {h['headline']}")


# ============================================================================
# Price / change / warrant filtering for screener results
# ============================================================================
def filter_gainers(gainers, min_change=20.0, max_price=22.0, min_price=1.0,
                   exclude_warrants=True):
    """Apply filters to the raw screener results."""
    filtered = []
    for g in gainers:
        sym = g["symbol"]
        price = g["price"]
        change = g["change_pct"]

        if exclude_warrants and WARRANT_RE.search(sym):
            continue
        if change < min_change:
            continue
        if price > max_price:
            continue
        if price < min_price:
            continue
        filtered.append(g)

    return filtered
