# CLAUDE.md

## Project Overview

CNO +EV Scraper — scrapes positive expected value (+EV) sports bets from [CrazyNinjaOdds](https://crazyninjaodds.com/site/tools/positive-ev.aspx) using Playwright, with a Discord bot interface.

## Repository Structure

```
.
├── scrape_ev.py       # Core scraper — Playwright-based, CLI + importable API
├── discord_bot.py     # Discord bot — wraps scrape_ev for chat-based usage
├── requirements.txt   # Python deps: playwright, rich, tabulate, discord.py
├── .gitignore         # Ignores CSV output, debug files, .env, venv
└── README.md          # User-facing setup and usage docs
```

## Architecture

### scrape_ev.py

The scraper has two interfaces:
- **CLI**: `python scrape_ev.py [--flags]` — parses args, scrapes, prints table + writes CSV
- **Library**: `scrape_ev()` function — returns `(bets: list[dict], csv_path: Path)`, raises `RuntimeError` on failure

Key components:
- `apply_filters(page, args)` — best-effort DOM manipulation to set page filters (devig method, min EV, min books, mainlines)
- `detect_table(page)` — tries 6 selector strategies; returns `(strategy_name, headers, rows_data, url_rows)`. `url_rows` has the same shape as `rows_data` but contains the first `href` found in each cell.
- `rows_to_dicts(headers, rows, url_rows=None)` — normalizes ~40 header name variants to canonical fields. When `url_rows` is provided, stores URLs as `{field}_url` keys in each dict (e.g. `sportsbook_url`, `event_url`).
- `COLUMN_ALIASES` / `map_columns()` — normalizes header variants to canonical names
- `setup_api_intercept(page)` — optional XHR/fetch response capture for API discovery
- `write_csv()` / `print_table()` — output to timestamped CSV and rich console table

Canonical fields: `sport_league`, `event`, `game_time`, `market`, `bet_name`, `sportsbook`, `odds`, `fair_odds`, `ev_pct`, `kelly`

Extra fields captured if present in table: `books` (number of books with the line)

URL fields populated from table `<a>` hrefs: `sportsbook_url`, `event_url` (relative CNO URLs resolved to absolute)

### discord_bot.py

- Uses `discord.py` with `commands.Bot` (prefix: `!`)
- Runs `scrape_ev()` in a thread pool via `asyncio.run_in_executor` to avoid blocking
- Commands: `!ev`, `!evschedule <minutes>`, `!evschedule off`, `!evstop`
- Posts: embed summary (sportsbook breakdown, EV range), paginated bet embeds, CSV attachment
- Messages chunked to fit Discord's 2000-char limit

### Embed format (per bet)

Each bet field in the embed shows:
```
{Sport} • {Event}
➡ **{Pick}** ({Market})
💲 Odds: **{odds}** | Fair: **{fair_odds}**
📈 EV: **{ev}%** | Kelly: **${kelly_dollars}**
🏦 [{Sportsbook}]({sportsbook_url}) | Books: {books} | {game_time}
```

**Kelly dollar calculation:** computed by `_kelly_dollars(bet)` in `discord_bot.py` directly from `odds` and `fair_odds` fields — does NOT rely on the CNO kelly column.
- Formula: `kelly_frac = (b × p − (1−p)) / b` where `p` = true probability from fair_odds, `b` = profit-per-unit from book odds
- Dollar stake: `kelly_frac × KELLY_BANKROLL × KELLY_FRACTION`
- `KELLY_BANKROLL = 1000` (assumed bankroll)
- `KELLY_FRACTION = 0.15` (15% fractional Kelly)
- Both constants are defined at the top of `discord_bot.py` — change them there, not anywhere else.

**Sportsbook link:** uses `sportsbook_url` scraped from the CNO table's sportsbook column `<a>` href. This is the direct deep-link to place the bet, NOT the CNO event page link.

**Deduplication:** `_posted_bets` set in `discord_bot.py` tracks `(sportsbook, event, bet_name, market)` tuples. The auto-loop filters these out before posting so the same bet is never posted twice in a session. Cleared on bot restart.

## Development

### Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

### Running

```bash
# Scraper standalone
python scrape_ev.py
python scrape_ev.py --min-ev 2.0 --no-headless --intercept-api

# Discord bot
export DISCORD_TOKEN=...
python discord_bot.py
```

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `DISCORD_TOKEN` | For bot only | Discord bot token |
| `CNO_CHANNEL_ID` | No | Auto-post channel ID — if set, bot auto-arms the schedule on startup |
| `CNO_SCHEDULE_MINUTES` | No | Auto-post interval in minutes (default: 5) |
| `CNO_AUTO_MIN_EV` | No | Min EV% for scheduled scrapes (default: 6.0) |
| `CNO_AUTO_MIN_ODDS` | No | Min odds for scheduled scrapes, e.g. -150 (default: -150) |
| `CNO_AUTO_MAX_ODDS` | No | Max odds for scheduled scrapes, e.g. 150 (default: 150) |

### Two Operating Modes

**`!ev` command** — on-demand, uses all defaults (min EV 1%, odds -200 to +250). Controlled entirely by flags passed in Discord.

**Scheduled auto-post** — fires every `CNO_SCHEDULE_MINUTES`, uses tighter filters configured via env vars in the systemd service file:
- Currently: `CNO_AUTO_MIN_EV=6`, `CNO_AUTO_MIN_ODDS=-150`, `CNO_AUTO_MAX_ODDS=150`
- These MUST live in the service file, not in code — code changes should never affect them.

**Rule:** any time the bot's scheduled behavior is changed, update the service file env vars, not hardcoded defaults in the code.

### Output Files

| File | Generated by | Gitignored |
|------|-------------|------------|
| `ev_bets_YYYYMMDD_HHMMSS.csv` | scrape_ev | Yes |
| `csv_output/` | discord_bot | Yes |
| `debug_screenshot.png` | scrape_ev (on failure) | Yes |
| `debug_page.html` | scrape_ev (on failure) | Yes |
| `api_debug.json` | scrape_ev (`--intercept-api`) | Yes |

## Conventions

- **Python 3.10+** required (`BooleanOptionalAction` usage)
- **Logging**: use the `log` logger (`logging.getLogger`), not `print`, for operational messages
- **Selectors**: never hardcode CSS selectors without verifying against the live DOM — the site is ASP.NET with JS-rendered content that may change. Use the adaptive `TABLE_STRATEGIES` list and add new strategies as needed
- **Column mapping**: when the site changes column names, add entries to `COLUMN_ALIASES` rather than changing canonical field names
- **Playwright sync API** in scrape_ev.py (runs in thread pool when called from async bot)
- **No secrets in code** — `DISCORD_TOKEN` comes from env vars; `.env` is gitignored

## Common Tasks

### Adding a new table detection strategy

Add a dict to `TABLE_STRATEGIES` in `scrape_ev.py` with keys: `name`, `wait`, `headers`, `rows`, `cells`. Order matters — more common/specific strategies first.

### Adding a new canonical field

1. Add to `CANONICAL_FIELDS` list
2. Add header name variants to `COLUMN_ALIASES`
3. Update `format_discord_message()` in discord_bot.py if it should display

### Adding a new Discord command

Add a `@bot.command(name="...")` function in `discord_bot.py`. Long-running work should use `asyncio.run_in_executor`.

### Debugging scrape failures

Run with `--no-headless --intercept-api` to see the browser and capture API calls. On failure, check `debug_page.html` for the rendered DOM and `api_debug.json` for intercepted requests.
