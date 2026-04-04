# CNO +EV Scraper

Scrapes the Positive EV table from [CrazyNinjaOdds](https://crazyninjaodds.com/site/tools/positive-ev.aspx) using Playwright.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Usage

```bash
# Basic run with defaults
python scrape_ev.py

# Custom filters
python scrape_ev.py \
  --sportsbooks FanDuel DraftKings BetMGM \
  --min-ev 2.0 \
  --min-books 4 \
  --no-mainlines-only

# Intercept API calls (logs to api_debug.json)
python scrape_ev.py --intercept-api

# Run with visible browser for debugging
python scrape_ev.py --no-headless

# Custom Chromium path
python scrape_ev.py --chromium-path /path/to/chrome
```

## Default Filters

| Filter | Default |
|--------|---------|
| Sportsbooks | FanDuel, DraftKings, BetMGM, Caesars, BetRivers, Fanatics |
| Min EV% | 1% |
| Mainlines Only | Yes |
| Devig Method | Liquidity-Weighted Worst-case |
| Min Books | 3 |

## Output

- **Console**: Pretty-printed table (rich)
- **CSV**: `ev_bets_YYYYMMDD_HHMMSS.csv`

## Debugging

If the table doesn't load:
- `debug_screenshot.png` — page screenshot at failure
- `debug_page.html` — full rendered HTML
- `api_debug.json` — intercepted API calls (with `--intercept-api`)
