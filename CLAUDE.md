# CLAUDE.md

## READ THIS FIRST — Regression Rules

These rules exist because features have been broken by changes that didn't account for
existing behavior. Before touching any code, read the relevant section below.

**Never change these without explicit user instruction:**
- `KELLY_BANKROLL`, `KELLY_FRACTION`, `BIG_KELLY_BANKROLL`, `BIG_KELLY_FRACTION` — all env-var driven, defaults in code
- `_kelly_dollars()` calculation method — must use `odds` + `fair_odds`, NOT the CNO kelly column; picks bankroll/fraction based on whether sportsbook is in `BIG_BOOKS`
- `BIG_BOOKS` set and `_apply_auto_filters()` — big book splitting logic in the auto-post loop
- The embed field order and format (documented below)
- The `!ev` command defaults (min EV 1%, odds -200 to +250) — these are intentionally loose
- The scheduled auto-post filters — these live in the systemd service file as env vars, NOT in code
- The `_posted_bets` deduplication set — must remain in `_auto_post_loop`, not `!ev`
- The `on_ready` auto-arm logic — must use `CNO_CHANNEL_ID` + `CNO_SCHEDULE_MINUTES` env vars
- The `_auto_post_loop.error` handler — must stay so the loop survives exceptions
- The humanization helpers in `scrape_ev.py` — `_rdelay`, `_human_type`, `_hover_then_click`, `_simulate_reading`

**When adding any feature that changes bot behavior:**
1. Update this file in the same commit
2. If it's a tunable value, make it an env var, not a hardcoded default

---

## Project Overview

CNO +EV Scraper — scrapes positive expected value (+EV) sports bets from
[CrazyNinjaOdds](https://crazyninjaodds.com/site/tools/positive-ev.aspx) using
Playwright, with a Discord bot interface.

Deployed as a systemd service (`cno-bot.service`) on a Linux VPS. The service file
holds all deployment-specific configuration as `Environment=` lines.

## Repository Structure

```
.
├── scrape_ev.py       # Core scraper — Playwright-based, CLI + importable API
├── scrape_oa.py       # OddsAssist Pro scraper (secondary source, merged with CNO)
├── discord_bot.py     # Discord bot — wraps scrapers, all user-facing behavior
├── bet_tracker.py     # Bet result tracking — SQLite storage, ESPN auto-settlement, reporting
├── requirements.txt   # Python deps: playwright, rich, tabulate, discord.py
├── .gitignore         # Ignores CSV output, debug files, .env, venv, bet_tracker.db
└── README.md          # User-facing setup and usage docs
```

---

## Architecture

### scrape_ev.py — CNO Scraper

Two interfaces:
- **CLI**: `python scrape_ev.py [--flags]` — scrapes, prints table, writes CSV
- **Library**: `scrape_ev()` — returns `(bets: list[dict], csv_path: Path)`, raises `RuntimeError` on failure

Key functions and their contracts (do not change signatures without updating all callers):

| Function | Returns | Notes |
|---|---|---|
| `detect_table(page)` | `(strategy_name, headers, rows_data, url_rows)` | 4-tuple; `url_rows` same shape as `rows_data` but contains first `href` per cell |
| `rows_to_dicts(headers, rows, url_rows=None)` | `list[dict]` | URLs stored as `{field}_url` keys (e.g. `sportsbook_url`) |
| `apply_filters(page, args)` | None | Best-effort; uses humanized input helpers |
| `scrape_ev(...)` | `(bets, csv_path)` | Main entry point for library use |

**Canonical fields** (always present if the column exists on CNO):
`sport_league`, `event`, `game_time`, `market`, `bet_name`, `sportsbook`, `odds`, `fair_odds`, `ev_pct`, `kelly`

**Extra fields** captured if present: `books` (number of books with the line)

**URL fields** from table `<a>` hrefs: `sportsbook_url`, `event_url`
- Relative CNO URLs are resolved to absolute (`https://crazyninjaodds.com` + path)
- `sportsbook_url` = direct deep-link to place the bet on the book (use this in embeds)
- `event_url` = CNO event page (do NOT use this as the bet link)

### scrape_oa.py — OddsAssist Scraper

Secondary source. Called in parallel with CNO by `run_scrape_async()`. Results are
merged and tagged with `source="OA"` (CNO bets get `source="CNO"`). The `[CNO]` /
`[OA]` tag appears at the end of the sportsbook line in embeds.

### discord_bot.py — Bot

Uses `discord.py` `commands.Bot` (prefix: `!`). All long-running work uses
`asyncio.run_in_executor` to avoid blocking the event loop.

**Commands:**

| Command | Behavior |
|---|---|
| `!ev [--min-ev N] [--min-odds N] [--max-odds N] [--sportsbooks ...]` | On-demand scrape. Uses loose defaults (min EV 1%, odds -200 to +250). Posts embeds + CSV. |
| `!evschedule <minutes>` | Start/change auto-post interval. Minimum 5 min. |
| `!evschedule off` | Stop auto-posting. |
| `!evstop` | Same as `!evschedule off`. |
| `!parlay [N] [--legs N] [--sport X] [--book X] [--same-game] [--count N]` | Build N-leg parlays from current bets. |
| `!ask <question>` | Chat with Gemini AI assistant (sports betting context). |
| `!clearchat` | Clear AI conversation history for the channel. |
| `!record` | Show overall W/L record, ROI, and profit breakdown by sportsbook. |
| `!pending` | List up to 10 oldest unsettled tracked bets with their IDs. |
| `!settle <id> win\|loss\|push\|void` | Manually settle a tracked bet by ID. |
| `!settlecheck` | Trigger an immediate ESPN auto-settlement pass on all pending bets. |
| `!manualreview` | Show breakdown of needs-manual bets by market type and sport — use to identify gaps in auto-settlement coverage. |

---

## Embed Format (per bet) — DO NOT CHANGE without instruction

Each bet appears as a Discord embed field:

```
{Sport} • {Event}
➡ **{Pick}** ({Market})
💲 Odds: **{odds}** | Fair: **{fair_odds}**
📈 EV: **{ev}%** | Kelly: **${kelly_dollars}**
🏦 [{Sportsbook}]({sportsbook_url}) | Books: {books} | {game_time} [{source}]
```

- The sportsbook name is a **clickable hyperlink** using `sportsbook_url` (the bet deep-link)
- `Books:` count comes from the `books` field scraped from the table
- `[CNO]` or `[OA]` source tag appears after game_time
- Kelly line only shows if Kelly can be calculated; Books/time only show if present

### Kelly Dollar Calculation — DO NOT CHANGE

Calculated by `_kelly_dollars(bet)` in `discord_bot.py` using **`odds` and `fair_odds`
directly** — does NOT use the CNO `kelly` column (it's unreliable/variably formatted).

```
fair_prob  = implied probability from fair_odds (American)
b          = profit-per-unit from book odds (American)
kelly_frac = (b × fair_prob − (1 − fair_prob)) / b
kelly_$    = kelly_frac × bankroll × fraction
```

The function picks `bankroll` and `fraction` based on whether the bet's sportsbook is
in `BIG_BOOKS`. All four values are env-var driven (service file), defaulting to 0.25:

| Env var | Default | Description |
|---|---|---|
| `CNO_KELLY_BANKROLL` | `1000` | Assumed bankroll for regular books |
| `CNO_KELLY_FRACTION` | `0.25` | Fractional Kelly for regular books |
| `CNO_BIG_KELLY_BANKROLL` | `1000` | Assumed bankroll for big books |
| `CNO_BIG_KELLY_FRACTION` | `0.25` | Fractional Kelly for big books |

---

## Two Operating Modes — DO NOT CONFLATE

### Mode 1: `!ev` (on-demand)
- Triggered manually in Discord
- Uses **loose defaults**: min EV 1%, odds -200 to +250, all default sportsbooks
- No deduplication — shows everything passing the filter right now
- User can override any filter with flags in the command

### Mode 2: Scheduled auto-post
- Fires every `CNO_SCHEDULE_MINUTES` minutes (default: 5)
- One scrape per run using the most permissive filters across both categories, then
  split and filtered in code by `_apply_auto_filters()`
- **Big books** (FanDuel, DraftKings by default — set via `CNO_BIG_BOOKS`):
  - Tighter thresholds: `CNO_BIG_MIN_EV=8`, same odds/books as regular by default
  - Posted first with an `@role` mention if `CNO_BIG_ROLE_ID` is set
- **Regular books** (everything not in BIG_BOOKS):
  - `CNO_AUTO_MIN_EV=7`, `CNO_AUTO_MIN_ODDS=-150`, `CNO_AUTO_MAX_ODDS=150`, `CNO_AUTO_MIN_BOOKS=4`
  - Posted after big books, no role mention
- **Deduplication active**: `_posted_bets` set tracks `(sportsbook, event, bet_name, market)`
  tuples already sent this session. A bet posted in run N will not appear in run N+1, N+2, etc.
  The set clears on bot restart.
- Auto-arms itself on startup via `on_ready` when `CNO_CHANNEL_ID` is set

**Rule: scheduled behavior is configured via service file env vars, never hardcoded.**
If the user asks to change scheduled filters, update the env vars in the service file
and document the new values here — do not change code defaults.

---

## Scheduler / Auto-Arm

`on_ready` checks for `CNO_CHANNEL_ID` and `AUTO_SCHEDULE_MINUTES > 0`. If both are
set, it configures `_auto_post_loop._channel_id` and starts the loop automatically.
This means the bot **never needs `!evschedule` after a restart**.

The loop is protected against silent death by:
1. All scrape and send logic inside a single `try/except` — any failure is caught and
   reported to the channel (with a nested try/except on the error send itself)
2. `@_auto_post_loop.error` handler — logs unhandled exceptions without stopping the loop

---

## Humanization (scrape_ev.py)

The scraper mimics human browser behavior to avoid bot detection. These helpers exist
at module level and are used throughout `apply_filters` and `scrape_ev()`:

| Helper | Purpose |
|---|---|
| `_rdelay(page, lo_ms, hi_ms)` | Random wait between lo and hi milliseconds |
| `_human_type(element, text)` | Triple-click to select, then type with 60–160ms per-key delay |
| `_hover_then_click(page, locator)` | Move mouse to element with slight random offset, then click |
| `_simulate_reading(page)` | 2–4 random scroll steps after page load, mimics skimming |

Each browser session also randomizes:
- **User-agent**: picked from `_USER_AGENTS` pool (Chrome 130/131 on Win/Mac, Edge)
- **Viewport**: picked from `_VIEWPORTS` pool (1280×800 up to 1920×1080)
- **Headers**: `Accept-Language: en-US`, `Referer: https://www.google.com/`
- **Locale / timezone**: `en-US` / `America/New_York`
- **Pre-navigation pause**: 500–2000ms random delay before `page.goto()`

The auto-post loop also adds 0–45s of random jitter before each scrape so runs never
fire at exact clock boundaries.

The loop silently skips any run between **midnight and 6 AM ET** (checked via
`America/New_York` timezone). The loop itself keeps ticking — it just does nothing
during those hours and resumes automatically at 6 AM.

---

## Environment Variables (full list)

All deployment-specific config lives in `/etc/systemd/system/cno-bot.service`.

| Variable | Default in code | Current service value | Description |
|---|---|---|---|
| `DISCORD_TOKEN` | — | (secret) | Discord bot token |
| `GEMINI_API_KEY` | — | (secret) | Gemini AI API key for `!ask` |
| `CNO_CHANNEL_ID` | `""` | `1490030828527554622` | Primary channel to auto-post to; also triggers auto-arm on startup |
| `CNO_MIRROR_CHANNEL_ID` | `""` | (second channel ID) | Optional second channel to mirror posts to (no role ping; bot must be in that server) |
| `CNO_SCHEDULE_MINUTES` | `5` | `5` | Auto-post interval in minutes |
| **Regular book filters** | | | |
| `CNO_AUTO_MIN_EV` | `1.0` | `7.0` | Min EV% for regular book scheduled scrapes |
| `CNO_AUTO_MIN_ODDS` | `-200` | `-150` | Min odds for regular book scheduled scrapes |
| `CNO_AUTO_MAX_ODDS` | `250` | `150` | Max odds for regular book scheduled scrapes |
| `CNO_AUTO_MIN_BOOKS` | `3` | `4` | Min books with the line for regular book scrapes |
| **Big book filters** | | | |
| `CNO_BIG_BOOKS` | `FanDuel,DraftKings` | `FanDuel,DraftKings` | Comma-separated list of big books (case-insensitive) |
| `CNO_BIG_MIN_EV` | `8.0` | `8.0` | Min EV% for big book scheduled scrapes |
| `CNO_BIG_MIN_ODDS` | `-200` | `-150` | Min odds for big book scheduled scrapes |
| `CNO_BIG_MAX_ODDS` | `250` | `150` | Max odds for big book scheduled scrapes |
| `CNO_BIG_MIN_BOOKS` | `3` | `4` | Min books with the line for big book scrapes |
| `CNO_BIG_ROLE_ID` | `""` | (your role ID) | Discord role ID to @mention on big book posts |
| **Kelly settings** | | | |
| `CNO_KELLY_BANKROLL` | `1000` | `1000` | Assumed bankroll for regular book Kelly calc |
| `CNO_KELLY_FRACTION` | `0.25` | `0.25` | Fractional Kelly multiplier for regular books |
| `CNO_BIG_KELLY_BANKROLL` | `1000` | `1000` | Assumed bankroll for big book Kelly calc |
| `CNO_BIG_KELLY_FRACTION` | `0.25` | `0.25` | Fractional Kelly multiplier for big books |
| **Bet tracker** | | | |
| `CNO_TRACKER_DB` | `./bet_tracker.db` | (default) | Path to SQLite file for bet result tracking |
| `CNO_ESPN_TIMEOUT_S` | `10` | (default) | Seconds before ESPN API requests time out |
| `CNO_SETTLE_ON_TIMER` | `1` | (default) | Set to `0` to disable auto-settlement on the loop timer |

When adding new tunable behavior, add it here as an env var with a sensible code
default, and record the current service value in this table.

---

## Development Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

## Running Locally

```bash
# Scraper standalone
python scrape_ev.py
python scrape_ev.py --min-ev 2.0 --no-headless --intercept-api

# Discord bot
export DISCORD_TOKEN=...
python discord_bot.py
```

## Output Files (all gitignored)

| File | Generated by | Notes |
|---|---|---|
| `ev_bets_YYYYMMDD_HHMMSS.csv` | scrape_ev | Per-run CSV |
| `csv_output/` | discord_bot | Bot CSV output dir |
| `debug_screenshot.png` | scrape_ev (on failure) | Check when scrape fails |
| `debug_page.html` | scrape_ev (on failure) | Full rendered DOM |
| `api_debug.json` | scrape_ev (`--intercept-api`) | Captured XHR responses |

---

## Conventions

- **Python 3.10+** required (`BooleanOptionalAction` usage)
- **Logging**: use the `log` logger (`logging.getLogger`), not `print`
- **Selectors**: never hardcode CSS selectors — the CNO site is ASP.NET with JS-rendered
  content that changes. Use `TABLE_STRATEGIES` and add new strategies as needed.
- **Column mapping**: when CNO renames columns, add to `COLUMN_ALIASES` — never rename
  canonical field names
- **Playwright sync API** in scrape_ev.py (called via thread pool from async bot)
- **No secrets in code** — all tokens/keys come from env vars

---

## Common Tasks

### Changing the scheduled filter values (EV threshold, odds range)
Edit the service file on the server, not the code:
```bash
sudo nano /etc/systemd/system/cno-bot.service
# Edit CNO_AUTO_MIN_EV, CNO_AUTO_MIN_ODDS, CNO_AUTO_MAX_ODDS, CNO_AUTO_MIN_BOOKS
sudo systemctl daemon-reload && sudo systemctl restart cno-bot
```
Then update the "Current service value" column in the env vars table above.

### Changing Kelly bankroll or fraction
Edit `KELLY_BANKROLL` and `KELLY_FRACTION` at the top of `discord_bot.py`.
Update the values documented in the "Kelly Dollar Calculation" section above.

### Adding a new embed field
1. Ensure the scraper captures it (add to `COLUMN_ALIASES` if it's a new column)
2. Read it in `format_bet_embeds()` via `bet.get("field_name", "")`
3. Add it to the embed format diagram in this file

### Adding a new table detection strategy
Add a dict to `TABLE_STRATEGIES` in `scrape_ev.py`:
`{"name": "...", "wait": "...", "headers": "...", "rows": "...", "cells": "..."}`.
More specific strategies should come first.

### Adding a new canonical field
1. Add to `CANONICAL_FIELDS` list in `scrape_ev.py`
2. Add header name variants to `COLUMN_ALIASES`
3. Update `format_bet_embeds()` in `discord_bot.py` if it should display
4. Update the embed format diagram in this file

### Adding a new Discord command
Add a `@bot.command(name="...")` function in `discord_bot.py`.
Long-running work must use `asyncio.run_in_executor`. Document it in the Commands table above.

### Debugging scrape failures
```bash
python scrape_ev.py --no-headless --intercept-api
```
On failure: `debug_page.html` has the rendered DOM, `api_debug.json` has XHR calls.

### Pulling updates to the server
```bash
git pull origin <branch>
sudo systemctl restart cno-bot
sudo journalctl -u cno-bot -f   # watch logs
```
