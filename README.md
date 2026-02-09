# Stock Screener

A real-time stock market screener that identifies high-momentum gainers using the Alpaca Markets API. Filters out warrants, Chinese shell companies (via SEC EDGAR), and noise — surfaces only stocks with real news catalysts.

Built for day traders who need a clean watchlist before market open.

## What It Does

1. **Pulls top gainers** from Alpaca's market movers API
2. **Filters by price and % change** — configurable thresholds
3. **Removes warrants, units, and rights** — regex pattern matching on ticker symbols
4. **Flags Chinese shell companies** — cross-references SEC EDGAR filings (business address + state of incorporation)
5. **Checks for news catalysts** — queries Alpaca News API, filters out generic "market roundup" articles
6. **Fetches historical 1-min bar data** — optional, downloads via Alpaca SIP feed

## Pipeline

```
Alpaca Screener API (top 20 gainers)
    → Price / % change filter ($1–$22, 20%+ move)
    → Warrant / unit exclusion (regex)
    → SEC EDGAR China filter (country + incorporation lookup)
    → News catalyst check (48h lookback, roundup filter)
    → Final watchlist
```

## Usage

```bash
# Basic scan — show today's top gainers with all filters
python3 screener.py

# Adjust filters
python3 screener.py --min-change 30 --max-price 10

# Hard filter — remove stocks with no news catalyst
python3 screener.py --news-hard

# Fetch 1-month of 1-min historical data for filtered stocks
python3 screener.py --fetch

# Skip specific filters
python3 screener.py --no-china-filter --no-news

# Pull more results
python3 screener.py --top 30
```

## Setup

```bash
# Clone the repo
git clone https://github.com/erictidmore/stock-screener.git
cd stock-screener

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure API keys
cp .env.example .env
# Edit .env with your Alpaca API credentials
```

Requires a free [Alpaca Markets](https://alpaca.markets) account.

## Filters

| Filter | Method | Source |
|--------|--------|--------|
| Price range | `$1.00 – $22.00` configurable | Alpaca screener |
| Min % change | `20%+` configurable | Alpaca screener |
| Warrants/units | Regex on ticker suffix (WS, WT, PR, U, R) | Ticker symbol |
| Chinese shells | Business address AND incorporation in CN/HK/Cayman/BVI | SEC EDGAR API |
| News catalyst | Company-specific headlines (filters roundups) | Alpaca News API |

## Tech Stack

- **Python 3.10+**
- **Alpaca Markets API** — screener, news, and historical data
- **SEC EDGAR API** — company domicile lookups
- **pandas** — data processing for historical bars

## Project Structure

```
stock-screener/
├── screener.py      # Main entry point — orchestrates the pipeline
├── filters.py       # All filter logic (warrants, china, news, price)
├── fetch.py         # Historical 1-min bar data downloader
├── config.py        # Configuration and API credential loading
├── requirements.txt
├── .env.example     # Template for API keys
└── data/            # Downloaded CSV data (gitignored)
```

## License

MIT
