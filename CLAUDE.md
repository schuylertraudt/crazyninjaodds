# CLAUDE.md

## Project Overview

+EV sports bet scraper with a Discord bot interface. Scrapes positive expected value bets from two sources:
- **CrazyNinjaOdds** (CNO) — `scrape_ev.py`, no auth required
- **OddsAssist Pro** (OA) — `scrape_oa.py`, requires persistent Google OAuth session

The Discord bot merges results from both sources and adds Gemini AI chat with natural-language scraping and parlay building.

## Repository Structure

```
.
├── scrape_ev.py       # CNO scraper — Playwright sync, CLI + importable library
├── scrape_oa.py       # OddsAssist scraper — persistent browser session, CLI + library
├── discord_bot.py     # Discord bot — dual scraper, Gemini AI, parlay builder
├── requirements.txt   # playwright, rich, tabulate, discord.py, google-generativeai
├── .gitignore
└── README.md
```

## Architecture

### scrape_ev.py (CrazyNinjaOdds)

Two interfaces:
- **CLI**: `python scrape_ev.py [--flags]`
- **Library**: `scrape_ev(...) -> (bets: list[dict], csv_path: Path)`, raises `RuntimeError` on failure

Key components:
- `apply_filters(page, args)` — best-effort DOM manipulation: devig method, min EV, min books, mainlines toggle
- `detect_table(page)` — tries 6 strategies in order: `html-table`, `ag-grid`, `role-grid`, `kendo-grid`, `dx-grid`, `div-table`
- `COLUMN_ALIASES` / `map_columns()` — normalizes ~40 raw header variants to 10 canonical fields
- `setup_api_intercept(page)` — optional XHR/fetch capture to `api_debug.json`
- `write_csv()` / `print_table()` — timestamped CSV + rich console table

Canonical fields: `sport_league`, `event`, `game_time`, `market`, `bet_name`, `sportsbook`, `odds`, `fair_odds`, `ev_pct`, `kelly`

Client-side filtering (applied after scrape): sportsbooks list, min EV%, odds range, junk row removal (requires non-empty event + odds + sportsbook).

**CLI flags:**
```
--sportsbooks         List (default: FanDuel DraftKings BetMGM Caesars BetRivers Fanatics)
--min-ev              Float (default: 1.0)
--mainlines-only      Bool via BooleanOptionalAction (default: True)
--devig-method        String (default: "Liquidity-Weighted Worst-case")
--min-books           Int (default: 3)
--max-odds            Int (default: 250)
--min-odds            Int (default: -200)
--output-dir          Path (default: ".")
--headless            Bool via BooleanOptionalAction (default: True)
--intercept-api       Flag — capture XHR/fetch to api_debug.json
--chromium-path       Custom Chromium executable path
```

### scrape_oa.py (OddsAssist Pro)

Same two interfaces as scrape_ev. Requires a saved browser session (persistent Chromium profile with Google OAuth).

Key differences from scrape_ev:
- Parses **card-based React layout** (not a table) via `_parse_bet_cards()` with 10 selector strategies; falls back to `_parse_from_text()`
- Uses persistent browser profile: `~/.oa_browser_profile` (or `OA_BROWSER_PROFILE` env var)
- Session must be initialized once via `python scrape_oa.py --login`
- Detects session expiry (redirect to login page) and raises `RuntimeError`
- CSV output: `oa_ev_bets_YYYYMMDD_HHMMSS.csv`
- Canonical fields include `source` field (value: `"OddsAssist"`)
- Default sportsbooks include **Hard Rock** in addition to CNO defaults

**CLI flags:** same as scrape_ev except no `--mainlines-only`, `--devig-method`, `--min-books`, or `--intercept-api`; adds `--login`.

### discord_bot.py

Uses `discord.py` `commands.Bot` (prefix: `!`). Long-running work runs via `asyncio.run_in_executor`.

**Commands:**

| Command | Description |
|---------|-------------|
| `!ev [args]` | Scrape +EV bets (CNO + OA in parallel), post embeds + CSV |
| `!parlay [N] [args]` | Build N-leg parlays (default 3) from latest bets |
| `!evschedule <minutes>` | Auto-post `!ev` every N minutes |
| `!evschedule off` | Cancel auto-post |
| `!evstop` | Cancel in-progress scrape |
| `!ask <question>` | Chat with Gemini AI; auto-triggers scrape/parlay via function calling |
| `!clearchat` | Clear per-channel Gemini conversation history |

**`!ev` flags:** `--min-ev`, `--min-books`, `--max-odds`, `--min-odds`, `--sportsbooks`, `--no-mainlines-only`

**`!parlay` flags:** `--legs <2-6>`, `--sport`, `--book`, `--same-game`, `--count <1-5>`

**Dual scraper flow** (`run_scrape_async`):
1. Runs `scrape_ev()` and `scrape_oa()` concurrently via thread pool
2. Tags each bet with `source` field (`"CNO"` or `"OddsAssist"`)
3. Merges and deduplicates; OA failures are non-fatal (logs warning)

**Parlay builder** (`build_parlays`):
- Takes top 20 bets by EV%, generates N-leg combinations
- Skips combinations with duplicate events (unless `--same-game`)
- Ranks by combined EV: `product(1 + ev/100 for each leg) - 1`
- Returns top `max_parlays` (default 3) by combined EV

**Gemini AI** (`!ask`):
- Model: `gemini-2.5-flash`
- System prompt: EV Ninja sports betting assistant (Kelly criterion, devig, strategy)
- Function declarations: `scrape_ev_bets`, `build_parlay`
- Per-channel conversation history, capped at 20 messages
- Rate-limit (429) retry: up to 3 attempts, exponential backoff (capped 60s)
- Fallback: if Gemini is unavailable and message matches bet/parlay regex, runs scraper directly

**Embed output:**
- Summary embed (top bets by EV%) + paginated bet embeds (max 10 per message, 10 bets per embed)
- Parlay embeds with legs, combined odds, combined EV, payout
- CSV attachment per scrape

## Development

### Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium

# One-time OddsAssist login (opens visible browser for Google OAuth)
python scrape_oa.py --login
```

### Running

```bash
# CNO scraper standalone
python scrape_ev.py
python scrape_ev.py --min-ev 2.0 --no-headless --intercept-api

# OddsAssist scraper standalone
python scrape_oa.py
python scrape_oa.py --min-ev 2.0 --no-headless

# Discord bot
export DISCORD_TOKEN=...
export GEMINI_API_KEY=...   # optional
python discord_bot.py
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | Yes (bot) | Discord bot token |
| `GEMINI_API_KEY` | No | Gemini API key — AI chat degrades gracefully if absent |
| `CNO_CHANNEL_ID` | No | Channel ID for auto-posting |
| `OA_BROWSER_PROFILE` | No | OddsAssist browser profile path (default: `~/.oa_browser_profile`) |

### Output Files

| File | Generated by | Gitignored |
|------|-------------|------------|
| `ev_bets_YYYYMMDD_HHMMSS.csv` | scrape_ev | Yes |
| `oa_ev_bets_YYYYMMDD_HHMMSS.csv` | scrape_oa | Yes |
| `csv_output/` | discord_bot | Yes |
| `debug_screenshot.png` | scrape_ev (on failure) | Yes |
| `debug_page.html` | scrape_ev (on failure) | Yes |
| `debug_oa_screenshot.png` | scrape_oa (on failure) | Yes |
| `debug_oa_page.html` | scrape_oa (on failure) | Yes |
| `api_debug.json` | scrape_ev (`--intercept-api`) | Yes |

## Conventions

- **Python 3.10+** required (`BooleanOptionalAction`)
- **Logging**: use the module-level `log = logging.getLogger(...)` logger, not `print`, for operational messages
- **Playwright sync API** in both scrapers — they're called inside `run_in_executor` from the async bot
- **Selectors**: never hardcode without verifying against the live DOM. CNO is ASP.NET with JS-rendered content; OA is React. Both can change at any time.
- **Column mapping**: when CNO changes column names, add entries to `COLUMN_ALIASES` — never rename canonical fields
- **OA card parsing**: when OA changes its card layout, update `_parse_bet_cards()` strategies or `_extract_bet_from_card_text()` — same principle as TABLE_STRATEGIES
- **No secrets in code** — all tokens/keys come from env vars; `.env` is gitignored
- **OA is non-fatal**: scrape_oa failures should be caught in `run_scrape_async` and logged as warnings, not bot crashes

## Common Tasks

### CNO: Adding a new table detection strategy

Add a dict to `TABLE_STRATEGIES` in `scrape_ev.py` with keys: `name`, `wait`, `headers`, `rows`, `cells`. Order matters — more specific strategies first.

### CNO: Site changes column names

Add entries to `COLUMN_ALIASES` in `scrape_ev.py`. Map new raw names to existing canonical fields. Never rename canonical fields — it breaks downstream consumers.

### OA: Site changes card layout

Update `_parse_bet_cards()` or `_extract_bet_from_card_text()` in `scrape_oa.py`. Test with `--no-headless` first. Check `debug_oa_page.html` on failures.

### Adding a new canonical field

1. Add to `CANONICAL_FIELDS` in the relevant scraper(s)
2. Add header/label variants to `COLUMN_ALIASES` (CNO) or `_extract_bet_from_card_text` (OA)
3. Update embed formatting in `discord_bot.py` if it should display

### Adding a new Discord command

Add a `@bot.command(name="...")` function in `discord_bot.py`. Long-running work must use `asyncio.run_in_executor`.

### Debugging scrape failures

**CNO:** Run with `--no-headless --intercept-api`. Check `debug_page.html` for rendered DOM, `api_debug.json` for intercepted requests.

**OA:** Run with `--no-headless`. If session expired, re-run `python scrape_oa.py --login`. Check `debug_oa_page.html`.

### OddsAssist session expired

```bash
python scrape_oa.py --login
# Complete Google OAuth in the browser window that opens
# Session is saved to ~/.oa_browser_profile automatically
```
