#!/usr/bin/env python3
"""
screener.py — Stock Market Screener
====================================
Pulls today's top gainers from Alpaca, applies filters (price, warrants,
Chinese shells, news catalysts), and optionally fetches historical data.

Usage:
    python3 screener.py                        # Show today's top gainers
    python3 screener.py --fetch                # Also download 1-min bar data
    python3 screener.py --top 30               # Pull top 30 gainers (default 20)
    python3 screener.py --min-change 20        # Min % change filter (default 20)
    python3 screener.py --max-price 22         # Max price filter (default 22)
    python3 screener.py --news-hard            # Remove stocks with no news catalyst
"""

import argparse
import sys

import config as cfg
from filters import (
    filter_gainers,
    filter_china_stocks,
    check_news_catalysts,
    filter_no_news,
    print_news_detail,
)
from fetch import fetch_data


# ---------------------------------------------------------------------------
# Screener: pull top gainers
# ---------------------------------------------------------------------------
def get_top_gainers(top: int = 20) -> tuple:
    """Pull top gainers from Alpaca's screener API."""
    from alpaca.data.historical.screener import ScreenerClient
    from alpaca.data.requests import MarketMoversRequest

    client = ScreenerClient(cfg.APCA_API_KEY_ID, cfg.APCA_API_SECRET_KEY)
    movers = client.get_market_movers(MarketMoversRequest(top=top))

    gainers = []
    for g in movers.gainers:
        gainers.append({
            "symbol": g.symbol,
            "price": g.price,
            "change_pct": g.percent_change,
            "change_dollar": g.change,
        })
    return gainers, movers.last_updated


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
def print_gainers(gainers, label=""):
    if label:
        print(f"\n  === {label} ===")
    has_news_info = any("news_catalyst" in g for g in gainers)
    if has_news_info:
        print(f"  {'#':<4} {'Symbol':<8} {'Price':>8} {'Change%':>10} {'Change$':>10}  {'News':<10}")
        print("  " + "-" * 56)
        for i, g in enumerate(gainers, 1):
            news_tag = ""
            if "news_catalyst" in g:
                if g["news_catalyst"] is True:
                    news_tag = "CATALYST"
                elif g["news_catalyst"] is False:
                    news_tag = "NO NEWS"
                else:
                    news_tag = "?"
            print(f"  {i:<4} {g['symbol']:<8} ${g['price']:>7.2f} "
                  f"{g['change_pct']:>+9.1f}% ${g['change_dollar']:>9.2f}  {news_tag}")
    else:
        print(f"  {'#':<4} {'Symbol':<8} {'Price':>8} {'Change%':>10} {'Change$':>10}")
        print("  " + "-" * 44)
        for i, g in enumerate(gainers, 1):
            print(f"  {i:<4} {g['symbol']:<8} ${g['price']:>7.2f} "
                  f"{g['change_pct']:>+9.1f}% ${g['change_dollar']:>9.2f}")
    print()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Stock Market Screener")
    parser.add_argument("--top", type=int, default=cfg.SCREENER_TOP,
                        help=f"Number of top movers to pull (default {cfg.SCREENER_TOP})")
    parser.add_argument("--min-change", type=float, default=cfg.SCREENER_MIN_CHANGE,
                        help=f"Min %% change to include (default {cfg.SCREENER_MIN_CHANGE})")
    parser.add_argument("--max-price", type=float, default=cfg.SCREENER_MAX_PRICE,
                        help=f"Max price filter (default {cfg.SCREENER_MAX_PRICE})")
    parser.add_argument("--min-price", type=float, default=cfg.SCREENER_MIN_PRICE,
                        help=f"Min price filter (default {cfg.SCREENER_MIN_PRICE})")
    parser.add_argument("--exclude-warrants", action="store_true", default=True,
                        help="Exclude warrants/units (default: yes)")
    parser.add_argument("--include-warrants", action="store_true",
                        help="Include warrants/units")
    parser.add_argument("--fetch", action="store_true",
                        help="Fetch 1-month of 1-min data for filtered symbols")
    parser.add_argument("--no-china-filter", action="store_true",
                        help="Skip Chinese stock filter (SEC EDGAR lookup)")
    parser.add_argument("--no-news", action="store_true",
                        help="Skip news check entirely")
    parser.add_argument("--news-hard", action="store_true",
                        help="Hard filter: remove stocks with no news (default: soft/tag only)")
    parser.add_argument("--news-hours", type=int, default=cfg.NEWS_LOOKBACK_HOURS,
                        help=f"How far back to check news in hours (default {cfg.NEWS_LOOKBACK_HOURS})")
    args = parser.parse_args()

    if not cfg.APCA_API_KEY_ID or not cfg.APCA_API_SECRET_KEY:
        print("ERROR: Set APCA_API_KEY_ID and APCA_API_SECRET_KEY in .env")
        sys.exit(1)

    exclude_warrants = not args.include_warrants

    # 1. Pull screener
    print("\n" + "=" * 70)
    print("  STOCK SCREENER — TOP GAINERS")
    print("=" * 70)

    raw_gainers, last_updated = get_top_gainers(args.top)
    print(f"  Last updated: {last_updated}")
    print(f"  Raw results: {len(raw_gainers)} gainers")

    print_gainers(raw_gainers, "RAW TOP GAINERS (unfiltered)")

    # 2. Price / change / warrant filter
    filtered = filter_gainers(
        raw_gainers,
        min_change=args.min_change,
        max_price=args.max_price,
        min_price=args.min_price,
        exclude_warrants=exclude_warrants,
    )

    if not filtered:
        print("  No symbols passed filters. Try lowering --min-change or raising --max-price.\n")
        return

    print_gainers(filtered, f"FILTERED ({args.min_change}%+ change, ${args.min_price}-${args.max_price} price)")

    # 3. China stock filter (SEC EDGAR)
    if not args.no_china_filter:
        pre_count = len(filtered)
        filtered = filter_china_stocks(filtered)
        if not filtered:
            print("  All symbols removed by China filter. Use --no-china-filter to disable.\n")
            return
        if len(filtered) < pre_count:
            print_gainers(filtered, "AFTER CHINA FILTER")

    # 4. News catalyst check
    if not args.no_news:
        filtered = check_news_catalysts(filtered, hours=args.news_hours)
        if args.news_hard:
            pre_count = len(filtered)
            filtered = filter_no_news(filtered)
            if not filtered:
                print("  All symbols removed by news filter. Use without --news-hard to keep all.\n")
                return
            if len(filtered) < pre_count:
                print_gainers(filtered, "AFTER NEWS FILTER (hard)")
        else:
            print_gainers(filtered, "WITH NEWS CHECK (soft — all kept)")

        print_news_detail(filtered)

    symbols = [g["symbol"] for g in filtered]
    print(f"\n  Watchlist ({len(symbols)}): {', '.join(symbols)}\n")

    # 5. Fetch historical data
    if args.fetch:
        print("=" * 70)
        print("  FETCHING 1-MONTH DATA")
        print("=" * 70)
        fetch_data(symbols)


if __name__ == "__main__":
    main()
