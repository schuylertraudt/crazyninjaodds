#!/usr/bin/env python3
"""
Discord bot that scrapes +EV bets from CrazyNinjaOdds and posts them to a channel.

Setup:
  1. Create a Discord application at https://discord.com/developers/applications
  2. Go to Bot → create a bot, copy the token
  3. Go to OAuth2 → URL Generator, select scopes: bot, permissions: Send Messages, Attach Files
  4. Use the generated URL to invite the bot to your server
  5. Set DISCORD_TOKEN env var (or put it in .env)
  6. Optionally set CNO_CHANNEL_ID to auto-post to a specific channel

Usage:
  python discord_bot.py                      # Run the bot
  DISCORD_TOKEN=xxx python discord_bot.py    # With inline token

Commands (in Discord):
  !ev              - Scrape now with default filters
  !ev --min-ev 2   - Scrape with custom min EV%
  !ev --sportsbooks FanDuel DraftKings
  !ev --help       - Show filter options
  !parlay          - Build 3-leg parlays from current +EV bets
  !parlay 4        - Build 4-leg parlays
  !parlay --sport NBA --book FanDuel
  !parlay --help   - Show parlay options
  !evstop          - Cancel a running scrape
  !evschedule 15   - Auto-post every 15 minutes
  !evschedule off  - Stop auto-posting
"""

import asyncio
import json
import logging
import math
import os
import random
import re
import shlex
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from itertools import combinations
from pathlib import Path

import discord
from discord.ext import commands, tasks

from scrape_ev import (
    DEFAULT_DEVIG,
    DEFAULT_MAX_ODDS,
    DEFAULT_MIN_BOOKS,
    DEFAULT_MIN_EV,
    DEFAULT_MIN_ODDS,
    DEFAULT_SPORTSBOOKS,
    scrape_ev,
)
from scrape_oa import scrape_oa

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cno-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
AUTO_CHANNEL_ID = os.environ.get("CNO_CHANNEL_ID", "")
MIRROR_CHANNEL_ID = os.environ.get("CNO_MIRROR_CHANNEL_ID", "")
AUTO_SCHEDULE_MINUTES = int(os.environ.get("CNO_SCHEDULE_MINUTES", "5"))

# Kelly Criterion — configurable per category; defaults to 25% fractional Kelly
KELLY_BANKROLL = int(os.environ.get("CNO_KELLY_BANKROLL", "1000"))
KELLY_FRACTION = float(os.environ.get("CNO_KELLY_FRACTION", "0.25"))
BIG_KELLY_BANKROLL = int(os.environ.get("CNO_BIG_KELLY_BANKROLL", "1000"))
BIG_KELLY_FRACTION = float(os.environ.get("CNO_BIG_KELLY_FRACTION", "0.25"))

# Big books — FanDuel and DraftKings get separate (higher) thresholds and a role ping
BIG_BOOKS = {
    b.strip().lower()
    for b in os.environ.get("CNO_BIG_BOOKS", "FanDuel,DraftKings").split(",")
    if b.strip()
}
BIG_MIN_EV = float(os.environ.get("CNO_BIG_MIN_EV", "8.0"))
BIG_MIN_ODDS = int(os.environ.get("CNO_BIG_MIN_ODDS", str(DEFAULT_MIN_ODDS)))
BIG_MAX_ODDS = int(os.environ.get("CNO_BIG_MAX_ODDS", str(DEFAULT_MAX_ODDS)))
BIG_MIN_BOOKS = int(os.environ.get("CNO_BIG_MIN_BOOKS", str(DEFAULT_MIN_BOOKS)))
BIG_ROLE_ID = os.environ.get("CNO_BIG_ROLE_ID", "")

# Scheduled-scrape filters for all other books — override via env vars in the service file
AUTO_MIN_EV = float(os.environ.get("CNO_AUTO_MIN_EV", str(DEFAULT_MIN_EV)))
AUTO_MIN_ODDS = int(os.environ.get("CNO_AUTO_MIN_ODDS", str(DEFAULT_MIN_ODDS)))
AUTO_MAX_ODDS = int(os.environ.get("CNO_AUTO_MAX_ODDS", str(DEFAULT_MAX_ODDS)))
AUTO_MIN_BOOKS = int(os.environ.get("CNO_AUTO_MIN_BOOKS", str(DEFAULT_MIN_BOOKS)))
OUTPUT_DIR = Path("./csv_output")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Tracks the running scrape task so it can be cancelled
_scrape_task = None
_schedule_interval = None

# Deduplication: bets already posted by the auto-loop this session
# Key: (sportsbook, event, bet_name, market) — cleared on restart
_posted_bets: set = set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


BOOK_EMOJI = {
    "fanduel": "<:fd:1>",
    "draftkings": "<:dk:2>",
    "betmgm": "<:mgm:3>",
    "caesars": "<:czr:4>",
    "betrivers": "<:br:5>",
    "fanatics": "<:fan:6>",
}
# Fallback if custom emojis aren't set up — uses text badges instead
BOOK_BADGE = {
    "fanduel": "FD",
    "draftkings": "DK",
    "betmgm": "MGM",
    "caesars": "CZR",
    "betrivers": "BR",
    "bet365": "B365",
    "thescore bet": "SCR",
    "fanatics": "FAN",
    "hard rock": "HR",
}


def _ev_sort_key(bet):
    """Parse EV% string to float for sorting."""
    raw = bet.get("ev_pct", "0").replace("%", "").replace("+", "").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _kelly_dollars(bet):
    """Calculate fractional Kelly stake in dollars from fair_odds + book odds.
    Uses KELLY_BANKROLL and KELLY_FRACTION constants.
    Returns a formatted string like '$11.59', or '' if odds can't be parsed."""
    try:
        odds_str = str(bet.get("odds", "")).replace("+", "").strip()
        fair_str = str(bet.get("fair_odds", "")).replace("+", "").strip()
        # Strip any parenthetical or non-numeric suffixes
        odds_str = re.sub(r"[^0-9\-]", "", odds_str)
        fair_str = re.sub(r"[^0-9\-]", "", fair_str)
        if not odds_str or not fair_str:
            return ""
        book_odds = int(odds_str)
        fair_odds = int(fair_str)
        # Convert fair American odds → true probability
        if fair_odds > 0:
            fair_prob = 100 / (100 + fair_odds)
        else:
            fair_prob = abs(fair_odds) / (abs(fair_odds) + 100)
        # Convert book American odds → decimal profit-per-unit
        if book_odds > 0:
            b = book_odds / 100
        else:
            b = 100 / abs(book_odds)
        kelly_frac = (b * fair_prob - (1 - fair_prob)) / b
        if kelly_frac <= 0:
            return ""
        is_big = bet.get("sportsbook", "").strip().lower() in BIG_BOOKS
        bankroll = BIG_KELLY_BANKROLL if is_big else KELLY_BANKROLL
        fraction = BIG_KELLY_FRACTION if is_big else KELLY_FRACTION
        dollars = kelly_frac * bankroll * fraction
        return f"${dollars:.2f}"
    except (ValueError, TypeError, ZeroDivisionError):
        return ""


def _book_badge(name):
    """Short badge for a sportsbook name."""
    return BOOK_BADGE.get(name.lower(), name[:3].upper())


def _ev_bar(ev_val):
    """Visual bar for EV% — easier to scan on mobile."""
    try:
        ev = float(str(ev_val).replace("%", "").replace("+", "").strip())
    except (ValueError, TypeError):
        return ""
    blocks = min(int(ev), 10)
    return "\u2588" * blocks + "\u2591" * (10 - blocks)


# ---------------------------------------------------------------------------
# Parlay builder
# ---------------------------------------------------------------------------


def _american_to_decimal(odds_str):
    """Convert American odds string to decimal odds."""
    cleaned = str(odds_str).replace("+", "").strip()
    try:
        odds = int(cleaned)
    except (ValueError, TypeError):
        return None
    if odds > 0:
        return (odds / 100) + 1
    elif odds < 0:
        return (100 / abs(odds)) + 1
    return None


def _decimal_to_american(dec):
    """Convert decimal odds to American odds string."""
    if dec >= 2.0:
        return f"+{round((dec - 1) * 100)}"
    elif dec > 1.0:
        return f"{round(-100 / (dec - 1))}"
    return "+100"


def _calc_parlay(legs):
    """Calculate combined parlay from a list of bet dicts.
    Returns dict with parlay_odds (decimal), american_odds, implied_prob,
    combined_ev, and payout_per_dollar."""
    decimal_odds = []
    ev_values = []
    for leg in legs:
        dec = _american_to_decimal(leg.get("odds", ""))
        if dec is None:
            continue
        decimal_odds.append(dec)
        ev_str = leg.get("ev_pct", "0").replace("%", "").replace("+", "").strip()
        try:
            ev_values.append(float(ev_str))
        except ValueError:
            ev_values.append(0.0)

    if not decimal_odds:
        return None

    parlay_decimal = 1.0
    for d in decimal_odds:
        parlay_decimal *= d

    implied_prob = 1 / parlay_decimal if parlay_decimal > 0 else 0
    # Combined EV: product of (1 + ev/100) for each leg, minus 1
    combined_ev_factor = 1.0
    for ev in ev_values:
        combined_ev_factor *= (1 + ev / 100)
    combined_ev = (combined_ev_factor - 1) * 100

    return {
        "parlay_decimal": parlay_decimal,
        "american_odds": _decimal_to_american(parlay_decimal),
        "implied_prob": implied_prob * 100,
        "combined_ev": combined_ev,
        "payout_per_dollar": parlay_decimal,
        "num_legs": len(decimal_odds),
    }


def build_parlays(bets, num_legs=3, max_parlays=3, sport=None, sportsbook=None,
                  same_game=False):
    """Build optimal parlays from available +EV bets.

    Args:
        bets: list of bet dicts from the scraper
        num_legs: number of legs per parlay (2-6)
        max_parlays: max number of parlay suggestions to return
        sport: filter to a specific sport/league
        sportsbook: filter to a specific sportsbook
        same_game: if True, only combine bets from the same event

    Returns list of parlay dicts, each with 'legs' and 'stats'.
    """
    num_legs = max(2, min(num_legs, 6))

    # Filter bets that have valid odds
    valid = [b for b in bets if _american_to_decimal(b.get("odds", "")) is not None]

    if sport:
        sport_lower = sport.lower()
        valid = [b for b in valid if sport_lower in b.get("sport_league", "").lower()]

    if sportsbook:
        book_lower = sportsbook.lower()
        valid = [b for b in valid if book_lower in b.get("sportsbook", "").lower()]

    if same_game:
        # Group by event and only consider groups with enough legs
        by_event = {}
        for b in valid:
            event = b.get("event", "").strip()
            if event:
                by_event.setdefault(event, []).append(b)
        valid = []
        for event_bets in by_event.values():
            if len(event_bets) >= num_legs:
                valid.extend(event_bets)

    if len(valid) < num_legs:
        return []

    # Sort by EV% descending
    valid.sort(key=_ev_sort_key, reverse=True)

    # Limit combinations to avoid explosion — use top bets only
    pool = valid[:20]

    # Score all valid combinations
    scored = []
    seen_events = set()
    for combo in combinations(pool, num_legs):
        # Skip combos with duplicate events (can't parlay same game on most books)
        if not same_game:
            events = [b.get("event", "").strip() for b in combo]
            if len(set(events)) < len(events):
                continue

        stats = _calc_parlay(combo)
        if stats is None:
            continue

        scored.append({
            "legs": list(combo),
            "stats": stats,
        })

    # Sort by combined EV descending
    scored.sort(key=lambda p: p["stats"]["combined_ev"], reverse=True)

    return scored[:max_parlays]


def format_parlay_embed(parlay, index=1):
    """Create a Discord embed for a single parlay suggestion."""
    stats = parlay["stats"]
    legs = parlay["legs"]

    embed = discord.Embed(
        title=f"\U0001f3b0 Parlay #{index} — {stats['num_legs']} Legs",
        color=0xFFAA00,
    )

    # Parlay summary
    embed.add_field(
        name="Parlay Odds",
        value=f"**{stats['american_odds']}** ({stats['parlay_decimal']:.1f}x)",
        inline=True,
    )
    embed.add_field(
        name="Combined EV",
        value=f"**{stats['combined_ev']:.1f}%**",
        inline=True,
    )
    embed.add_field(
        name="$10 Payout",
        value=f"**${stats['payout_per_dollar'] * 10:.2f}**",
        inline=True,
    )

    # Individual legs
    for i, leg in enumerate(legs, 1):
        pick = leg.get("bet_name", "?")
        event = leg.get("event", "?")
        odds = leg.get("odds", "?")
        ev = leg.get("ev_pct", "?").replace("%", "").strip()
        book = leg.get("sportsbook", "?")
        market = leg.get("market", "")

        value = (
            f"\u27A1 **{pick}**" + (f" ({market})" if market else "") + "\n"
            f"\U0001f4b2 {odds} | EV: {ev}% | {book}"
        )
        embed.add_field(
            name=f"Leg {i}: {event}",
            value=value,
            inline=False,
        )

    embed.set_footer(text="Parlays are high-risk — EV compounds but so does variance")
    return embed


def format_parlay_embeds(parlays):
    """Create embeds for multiple parlay suggestions."""
    if not parlays:
        embed = discord.Embed(
            title="No Parlays Available",
            description="Not enough +EV bets to build parlays with those filters. Try broadening your criteria.",
            color=0xFF4444,
        )
        return [embed]

    return [format_parlay_embed(p, i + 1) for i, p in enumerate(parlays)]


def format_bet_embeds(bets, max_per_embed=10, max_embeds=4):
    """Create clean, mobile-friendly embeds — one embed per batch of bets,
    sorted by EV% descending."""
    if not bets:
        embed = discord.Embed(
            title="No +EV Bets Found",
            description="No bets matched your filters. Try lowering `--min-ev` or adding more sportsbooks.",
            color=0xFF4444,
        )
        return [embed]

    sorted_bets = sorted(bets, key=_ev_sort_key, reverse=True)
    now = datetime.now(ZoneInfo("America/New_York")).strftime("%b %d, %I:%M %p ET")

    # --- Summary embed ---
    summary = discord.Embed(
        title=f"\U0001f4b0 {len(bets)} +EV Bets Found",
        description=f"Scraped {now}",
        color=0x00CC66,
    )

    # Sportsbook breakdown
    books = {}
    for b in bets:
        sb = b.get("sportsbook", "").strip() or "Unknown"
        books[sb] = books.get(sb, 0) + 1
    book_lines = [f"{name}: {count}" for name, count in sorted(books.items(), key=lambda x: -x[1])]
    summary.add_field(name="Sportsbooks", value="\n".join(book_lines), inline=True)

    # EV range
    evs = [_ev_sort_key(b) for b in bets]
    if evs:
        summary.add_field(
            name="EV% Range",
            value=f"**{min(evs):.1f}%** \u2014 **{max(evs):.1f}%**",
            inline=True,
        )

    embeds = [summary]

    # --- Bet list embeds ---
    total_shown = max_per_embed * max_embeds
    for chunk_start in range(0, min(len(sorted_bets), total_shown), max_per_embed):
        chunk = sorted_bets[chunk_start : chunk_start + max_per_embed]
        page_num = chunk_start // max_per_embed + 1
        total_pages = min(
            (min(len(sorted_bets), total_shown) + max_per_embed - 1) // max_per_embed,
            max_embeds,
        )

        bet_embed = discord.Embed(
            color=0x2F3136,
        )
        if total_pages > 1:
            bet_embed.set_author(name=f"Page {page_num}/{total_pages}")

        for bet in chunk:
            log.info("Embed bet: %s", {k: v for k, v in bet.items() if k not in ('calc', 'extra')})
            sport = bet.get("sport_league", "").strip()
            event = bet.get("event", "").strip() or "\u2014"
            market = bet.get("market", "").strip()
            pick = bet.get("bet_name", "").strip() or "\u2014"
            odds = bet.get("odds", "").strip() or "\u2014"
            fair = bet.get("fair_odds", "").strip()
            ev = bet.get("ev_pct", "").strip() or "\u2014"
            book = bet.get("sportsbook", "").strip() or "\u2014"
            book_url = bet.get("sportsbook_url", "").strip()
            time_str = bet.get("game_time", "").strip()
            books_count = bet.get("books", "").strip()
            kelly_display = _kelly_dollars(bet)

            # Field name: event
            title = event
            if sport:
                title = f"{sport} \u2022 {event}"

            # Sportsbook line — use URL hyperlink if available
            book_link = f"[{book}]({book_url})" if book_url else book

            ev_display = str(ev).replace("%", "").replace("+", "").strip()
            source = bet.get("source", "").strip()
            source_tag = f" [{source}]" if source else ""
            lines = [
                f"\u27A1 **{pick}**" + (f" ({market})" if market else ""),
                f"\U0001f4b2 Odds: **{odds}**" + (f" | Fair: **{fair}**" if fair else ""),
                f"\U0001f4c8 EV: **{ev_display}%**"
                + (f" | Kelly: **{kelly_display}**" if kelly_display else ""),
                f"\U0001f3e6 {book_link}"
                + (f" | Books: {books_count}" if books_count else "")
                + (f" | {time_str}" if time_str else "")
                + source_tag,
            ]

            bet_embed.add_field(
                name=title,
                value="\n".join(lines),
                inline=False,
            )

        embeds.append(bet_embed)

    # Overflow note
    if len(bets) > total_shown:
        embeds[-1].set_footer(
            text=f"Showing top {total_shown} of {len(bets)} bets"
        )
    else:
        embeds[-1].set_footer(text="CrazyNinjaOdds +EV Scraper")

    return embeds


async def run_scrape_async(
    sportsbooks=None,
    min_ev=DEFAULT_MIN_EV,
    min_books=DEFAULT_MIN_BOOKS,
    max_odds=DEFAULT_MAX_ODDS,
    min_odds=DEFAULT_MIN_ODDS,
    devig_method=DEFAULT_DEVIG,
    mainlines_only=True,
):
    """Run both scrapers (CNO + OddsAssist) in parallel and merge results."""
    loop = asyncio.get_event_loop()
    books = sportsbooks or DEFAULT_SPORTSBOOKS

    # Run CNO scraper
    async def _run_cno():
        try:
            bets, csv_path = await loop.run_in_executor(
                None,
                lambda: scrape_ev(
                    sportsbooks=books,
                    min_ev=min_ev,
                    mainlines_only=mainlines_only,
                    devig_method=devig_method,
                    min_books=min_books,
                    max_odds=max_odds,
                    min_odds=min_odds,
                    headless=True,
                    output_dir=OUTPUT_DIR,
                ),
            )
            # Tag source
            for b in bets:
                b.setdefault("source", "CNO")
            return bets, csv_path
        except Exception as e:
            log.warning("CNO scraper failed: %s", e)
            return [], None

    # Run OddsAssist scraper (only if browser profile exists)
    async def _run_oa():
        try:
            bets, csv_path = await loop.run_in_executor(
                None,
                lambda: scrape_oa(
                    sportsbooks=books,
                    min_ev=min_ev,
                    max_odds=max_odds,
                    min_odds=min_odds,
                    headless=True,
                    output_dir=OUTPUT_DIR,
                ),
            )
            return bets, csv_path
        except Exception as e:
            log.warning("OddsAssist scraper failed: %s", e)
            return [], None

    # Run both in parallel
    cno_result, oa_result = await asyncio.gather(_run_cno(), _run_oa())
    cno_bets, cno_csv = cno_result
    oa_bets, oa_csv = oa_result

    # Merge results
    all_bets = cno_bets + oa_bets
    log.info("Combined results: %d CNO + %d OA = %d total",
             len(cno_bets), len(oa_bets), len(all_bets))

    # Use whichever CSV exists (prefer the one with more data)
    csv_path = cno_csv or oa_csv

    return all_bets, csv_path


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    log.info("Bot ready: %s (id=%s)", bot.user, bot.user.id)
    if AUTO_CHANNEL_ID and AUTO_SCHEDULE_MINUTES > 0:
        log.info(
            "Auto-starting schedule: every %d min → channel %s",
            AUTO_SCHEDULE_MINUTES, AUTO_CHANNEL_ID,
        )
        _auto_post_loop._channel_id = int(AUTO_CHANNEL_ID)
        if _auto_post_loop.is_running():
            _auto_post_loop.cancel()
        _auto_post_loop.change_interval(minutes=AUTO_SCHEDULE_MINUTES)
        _auto_post_loop.start()


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@bot.command(name="ev")
async def ev_command(ctx, *, raw_args: str = ""):
    """Scrape +EV bets. Use !ev --help for options."""
    global _scrape_task

    # Parse arguments from the message
    sportsbooks = DEFAULT_SPORTSBOOKS
    min_ev = DEFAULT_MIN_EV
    min_books = DEFAULT_MIN_BOOKS
    max_odds = DEFAULT_MAX_ODDS
    min_odds = DEFAULT_MIN_ODDS
    devig = DEFAULT_DEVIG
    mainlines = True

    if raw_args.strip() == "--help":
        help_text = (
            "**!ev options:**\n"
            "`--min-ev <float>` — Minimum EV% (default: 1.0)\n"
            "`--min-books <int>` — Minimum sportsbooks with line (default: 3)\n"
            "`--max-odds <int>` — Exclude odds above this (default: 250)\n"
            "`--min-odds <int>` — Exclude odds below this (default: -200)\n"
            "`--sportsbooks <book1> <book2> ...` — Filter to specific books\n"
            "`--no-mainlines-only` — Include props\n"
            "\n**Examples:**\n"
            "`!ev` — defaults\n"
            "`!ev --min-ev 2.5 --sportsbooks FanDuel DraftKings`\n"
            "`!ev --max-odds 200 --min-odds -150`\n"
        )
        await ctx.send(help_text)
        return

    if raw_args.strip():
        try:
            parts = shlex.split(raw_args)
            i = 0
            custom_books = []
            while i < len(parts):
                if parts[i] == "--min-ev" and i + 1 < len(parts):
                    min_ev = float(parts[i + 1])
                    i += 2
                elif parts[i] == "--min-books" and i + 1 < len(parts):
                    min_books = int(parts[i + 1])
                    i += 2
                elif parts[i] == "--max-odds" and i + 1 < len(parts):
                    max_odds = int(parts[i + 1])
                    i += 2
                elif parts[i] == "--min-odds" and i + 1 < len(parts):
                    min_odds = int(parts[i + 1])
                    i += 2
                elif parts[i] == "--sportsbooks":
                    i += 1
                    while i < len(parts) and not parts[i].startswith("--"):
                        custom_books.append(parts[i])
                        i += 1
                elif parts[i] == "--no-mainlines-only":
                    mainlines = False
                    i += 1
                else:
                    i += 1
            if custom_books:
                sportsbooks = custom_books
        except Exception as e:
            await ctx.send(f"Bad arguments: {e}\nUse `!ev --help` for usage.")
            return

    status_msg = await ctx.send(
        f"Scraping +EV bets (min EV: {min_ev}%, odds: {min_odds} to +{max_odds}) …"
    )

    try:
        bets, csv_path = await run_scrape_async(
            sportsbooks=sportsbooks,
            min_ev=min_ev,
            min_books=min_books,
            max_odds=max_odds,
            min_odds=min_odds,
            devig_method=devig,
            mainlines_only=mainlines,
        )
    except RuntimeError as e:
        await status_msg.edit(content=f"Scrape failed: {e}")
        return
    except Exception as e:
        log.exception("Scrape error")
        await status_msg.edit(content=f"Scrape error: {e}")
        return

    if not bets:
        await status_msg.edit(content="No +EV bets found matching your filters.")
        return

    # Post results as embeds
    await status_msg.edit(content=f"\u2705 Found **{len(bets)}** +EV bets!")

    embeds = format_bet_embeds(bets)
    # Discord allows max 10 embeds per message — send in batches
    for i in range(0, len(embeds), 10):
        await ctx.send(embeds=embeds[i : i + 10])


@bot.command(name="parlay")
async def parlay_command(ctx, *, raw_args: str = ""):
    """Build parlays from current +EV bets.

    Usage:
        !parlay                  - 3-leg parlay from all bets
        !parlay 4                - 4-leg parlay
        !parlay --legs 3 --sport NBA --book FanDuel
        !parlay --same-game      - same-game parlay
        !parlay --help
    """
    if raw_args.strip() == "--help":
        help_text = (
            "**!parlay options:**\n"
            "`!parlay` — build 3-leg parlays from current +EV bets\n"
            "`!parlay 4` — build 4-leg parlays\n"
            "`--legs <2-6>` — number of legs (default: 3)\n"
            "`--sport <name>` — filter to a sport (e.g. NBA, NFL, MLB)\n"
            "`--book <name>` — filter to a sportsbook\n"
            "`--same-game` — same-game parlay (all legs from one event)\n"
            "`--count <1-5>` — number of parlay suggestions (default: 3)\n"
            "\n**Examples:**\n"
            "`!parlay` — best 3-leg parlays\n"
            "`!parlay 4 --sport NBA`\n"
            "`!parlay --book FanDuel --legs 2`\n"
            "`!parlay --same-game`\n"
        )
        await ctx.send(help_text)
        return

    # Parse args
    num_legs = 3
    max_parlays = 3
    sport = None
    sportsbook = None
    same_game = False

    if raw_args.strip():
        try:
            parts = shlex.split(raw_args)
            i = 0
            while i < len(parts):
                if parts[i] == "--legs" and i + 1 < len(parts):
                    num_legs = int(parts[i + 1])
                    i += 2
                elif parts[i] == "--sport" and i + 1 < len(parts):
                    sport = parts[i + 1]
                    i += 2
                elif parts[i] == "--book" and i + 1 < len(parts):
                    sportsbook = parts[i + 1]
                    i += 2
                elif parts[i] == "--count" and i + 1 < len(parts):
                    max_parlays = min(int(parts[i + 1]), 5)
                    i += 2
                elif parts[i] == "--same-game":
                    same_game = True
                    i += 1
                elif parts[i].isdigit():
                    num_legs = int(parts[i])
                    i += 1
                else:
                    i += 1
        except Exception as e:
            await ctx.send(f"Bad arguments: {e}\nUse `!parlay --help` for usage.")
            return

    status_msg = await ctx.send(
        f"Scraping +EV bets and building {num_legs}-leg parlays..."
    )

    try:
        bets, csv_path = await run_scrape_async()
    except Exception as e:
        await status_msg.edit(content=f"Scrape failed: {e}")
        return

    if not bets:
        await status_msg.edit(content="No +EV bets found — can't build parlays.")
        return

    parlays = build_parlays(
        bets,
        num_legs=num_legs,
        max_parlays=max_parlays,
        sport=sport,
        sportsbook=sportsbook,
        same_game=same_game,
    )

    if not parlays:
        filters = []
        if sport:
            filters.append(f"sport={sport}")
        if sportsbook:
            filters.append(f"book={sportsbook}")
        if same_game:
            filters.append("same-game")
        filter_note = f" with filters: {', '.join(filters)}" if filters else ""
        await status_msg.edit(
            content=f"Found {len(bets)} bets but couldn't build {num_legs}-leg parlays{filter_note}. "
            "Try fewer legs or broader filters."
        )
        return

    await status_msg.edit(
        content=f"\u2705 Built **{len(parlays)}** parlay(s) from {len(bets)} +EV bets!"
    )

    embeds = format_parlay_embeds(parlays)
    for i in range(0, len(embeds), 10):
        await ctx.send(embeds=embeds[i : i + 10])


@bot.command(name="evstop")
async def evstop_command(ctx):
    """Cancel a running scheduled scrape loop."""
    global _schedule_interval
    if _auto_post_loop.is_running():
        _auto_post_loop.cancel()
        _schedule_interval = None
        await ctx.send("Auto-posting stopped.")
    else:
        await ctx.send("No auto-post is running.")


@bot.command(name="evschedule")
async def evschedule_command(ctx, interval: str = ""):
    """Schedule auto-posting. Usage: !evschedule 15  (minutes) or !evschedule off"""
    global _schedule_interval

    if not interval or interval.lower() == "off":
        if _auto_post_loop.is_running():
            _auto_post_loop.cancel()
            _schedule_interval = None
            await ctx.send("Auto-posting stopped.")
        else:
            await ctx.send("No auto-post is running. Usage: `!evschedule 15`")
        return

    try:
        minutes = int(interval)
        if minutes < 5:
            await ctx.send("Minimum interval is 5 minutes.")
            return
    except ValueError:
        await ctx.send("Usage: `!evschedule 15` or `!evschedule off`")
        return

    _schedule_interval = minutes
    # Store the channel to post to
    _auto_post_loop._channel_id = ctx.channel.id

    if _auto_post_loop.is_running():
        _auto_post_loop.cancel()

    _auto_post_loop.change_interval(minutes=minutes)
    _auto_post_loop.start()
    await ctx.send(f"Auto-posting every {minutes} minutes in this channel.")


def _apply_auto_filters(bets, min_ev, min_odds, max_odds, min_books):
    """In-code filter for per-category thresholds after scraping."""
    result = []
    for b in bets:
        try:
            ev = float(b.get("ev_pct", "0").replace("%", "").replace("+", "").strip())
        except (ValueError, TypeError):
            ev = 0.0
        try:
            odds = int(b.get("odds", "0").replace("+", "").strip())
        except (ValueError, TypeError):
            continue
        if ev < min_ev or not (min_odds <= odds <= max_odds):
            continue
        books_val = b.get("books", "").strip()
        if books_val:
            try:
                if int(books_val) < min_books:
                    continue
            except (ValueError, TypeError):
                pass
        result.append(b)
    return result


@tasks.loop(minutes=15)
async def _auto_post_loop():
    channel_id = getattr(_auto_post_loop, "_channel_id", None)
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return

    # Overnight pause — no scraping midnight to 6 AM ET
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.hour < 6:
        log.info("Auto-post: overnight pause (%s ET) — skipping.", now_et.strftime("%H:%M"))
        return

    # Random jitter so runs never land on exact clock boundaries
    jitter_s = random.uniform(0, 45)
    log.info("Auto-post: waiting %.0fs jitter before scrape …", jitter_s)
    await asyncio.sleep(jitter_s)

    # Scrape with the most permissive settings across both categories so nothing is missed
    scrape_min_ev = min(AUTO_MIN_EV, BIG_MIN_EV)
    scrape_min_odds = min(AUTO_MIN_ODDS, BIG_MIN_ODDS)
    scrape_max_odds = max(AUTO_MAX_ODDS, BIG_MAX_ODDS)
    scrape_min_books = min(AUTO_MIN_BOOKS, BIG_MIN_BOOKS)
    log.info(
        "Auto-post: scraping (floor: ev≥%.1f, odds %d..+%d, books≥%d) …",
        scrape_min_ev, scrape_min_odds, scrape_max_odds, scrape_min_books,
    )
    try:
        bets, csv_path = await run_scrape_async(
            min_ev=scrape_min_ev,
            min_odds=scrape_min_odds,
            max_odds=scrape_max_odds,
            min_books=scrape_min_books,
        )

        # Split into big books vs regular books, then apply per-category filters
        big_raw = [b for b in bets if b.get("sportsbook", "").strip().lower() in BIG_BOOKS]
        other_raw = [b for b in bets if b.get("sportsbook", "").strip().lower() not in BIG_BOOKS]
        big_bets = _apply_auto_filters(big_raw, BIG_MIN_EV, BIG_MIN_ODDS, BIG_MAX_ODDS, BIG_MIN_BOOKS)
        other_bets = _apply_auto_filters(other_raw, AUTO_MIN_EV, AUTO_MIN_ODDS, AUTO_MAX_ODDS, AUTO_MIN_BOOKS)

        def _bet_key(b):
            return (
                b.get("sportsbook", "").strip().lower(),
                b.get("event", "").strip().lower(),
                b.get("bet_name", "").strip().lower(),
                b.get("market", "").strip().lower(),
            )

        # Deduplicate both groups
        new_big = [b for b in big_bets if _bet_key(b) not in _posted_bets]
        new_other = [b for b in other_bets if _bet_key(b) not in _posted_bets]
        skipped = (len(big_bets) - len(new_big)) + (len(other_bets) - len(new_other))
        if skipped:
            log.info("Dedup: skipping %d already-posted bets", skipped)

        if not new_big and not new_other:
            log.info("Scheduled scrape: no new bets to post.")
            return

        # Mark as posted before sending
        for b in new_big + new_other:
            _posted_bets.add(_bet_key(b))

        # Build embeds once, reuse for all channels
        big_embeds = format_bet_embeds(new_big) if new_big else []
        other_embeds = format_bet_embeds(new_other) if new_other else []

        async def _send_results(dest, ping_role=False):
            if big_embeds:
                role_prefix = f"<@&{BIG_ROLE_ID}> " if (ping_role and BIG_ROLE_ID) else ""
                for i in range(0, len(big_embeds), 10):
                    content = f"{role_prefix}\U0001f3c6 Big book alert!" if i == 0 and role_prefix else None
                    await dest.send(content=content, embeds=big_embeds[i : i + 10])
            if other_embeds:
                for i in range(0, len(other_embeds), 10):
                    await dest.send(embeds=other_embeds[i : i + 10])

        log.info("Posting %d big + %d regular bets", len(new_big), len(new_other))
        await _send_results(channel, ping_role=True)

        if MIRROR_CHANNEL_ID:
            try:
                mirror = bot.get_channel(int(MIRROR_CHANNEL_ID)) or await bot.fetch_channel(int(MIRROR_CHANNEL_ID))
                log.info("Mirroring to channel %s", MIRROR_CHANNEL_ID)
                await _send_results(mirror, ping_role=False)
            except Exception as mirror_err:
                log.warning("Mirror channel %s not found or not accessible: %s", MIRROR_CHANNEL_ID, mirror_err)
    except Exception as e:
        log.exception("Scheduled scrape/post failed")
        try:
            await channel.send(f"Scheduled scrape failed: {e}")
        except Exception:
            log.warning("Could not send error message to channel either")


@_auto_post_loop.error
async def _auto_post_loop_error(error):
    """Log errors from the auto-post loop without stopping it."""
    log.exception("Unhandled error in auto-post loop — loop will continue", exc_info=error)


# ---------------------------------------------------------------------------
# Gemini AI Chat with Function Calling
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are EV Ninja, a knowledgeable sports betting assistant in a Discord server. "
    "You specialize in positive expected value (+EV) betting, devigging odds, "
    "Kelly criterion bankroll management, and sports betting strategy. "
    "Keep responses concise (under 1500 characters) since this is Discord. "
    "Use casual but informed tone. You can reference specific sports, markets, "
    "and sportsbooks. If someone asks about the bot's commands, mention: "
    "!ev (scrape bets), !evschedule (auto-post), !evstop, and !ask (chat with you). "
    "Never give financial advice — remind users that all betting carries risk.\n\n"
    "IMPORTANT: When a user asks to see, find, show, get, or check EV bets, +EV bets, "
    "or betting opportunities — USE the scrape_ev_bets tool to fetch live data. "
    "This includes requests like 'what EV bets are on FanDuel?', 'show me BetRivers bets', "
    "'any good bets right now?', 'what's out there on DraftKings?', etc. "
    "Extract sportsbook names and any filters from the user's message.\n\n"
    "IMPORTANT: When a user asks to build, make, create, or suggest a parlay — "
    "USE the build_parlay tool. Extract the number of legs, sport, sportsbook, "
    "and whether they want a same-game parlay from their message. "
    "Available sportsbooks: FanDuel, DraftKings, BetMGM, Caesars, BetRivers, Fanatics, Hard Rock."
)

# Gemini function declaration for the scraper tool
_SCRAPE_TOOL_DECLARATION = {
    "name": "scrape_ev_bets",
    "description": (
        "Scrape live +EV (positive expected value) sports bets from CrazyNinjaOdds and OddsAssist Pro. "
        "Call this whenever the user wants to see current EV bets, betting opportunities, "
        "or asks about what bets are available on specific sportsbooks."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "sportsbooks": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Sportsbooks to filter for. Valid values: FanDuel, DraftKings, "
                    "BetMGM, Caesars, BetRivers, Fanatics, Hard Rock. "
                    "Omit or pass empty array for all default sportsbooks."
                ),
            },
            "min_ev": {
                "type": "number",
                "description": "Minimum EV percentage to include (default 1.0).",
            },
            "max_odds": {
                "type": "integer",
                "description": "Exclude odds above this value, e.g. 250 means +250 (default 250).",
            },
            "min_odds": {
                "type": "integer",
                "description": "Exclude odds below this value, e.g. -200 (default -200).",
            },
        },
        "required": [],
    },
}

_PARLAY_TOOL_DECLARATION = {
    "name": "build_parlay",
    "description": (
        "Build optimized parlays from current +EV bets. Scrapes live bets and combines "
        "the best ones into parlay suggestions. Call this when the user asks to build, "
        "make, create, or suggest a parlay."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "num_legs": {
                "type": "integer",
                "description": "Number of legs in the parlay (2-6, default 3).",
            },
            "sport": {
                "type": "string",
                "description": "Filter to a specific sport/league (e.g. NBA, NFL, MLB, NHL, EPL).",
            },
            "sportsbook": {
                "type": "string",
                "description": "Filter to a specific sportsbook (e.g. FanDuel, DraftKings).",
            },
            "same_game": {
                "type": "boolean",
                "description": "If true, all legs must be from the same event (same-game parlay).",
            },
            "max_parlays": {
                "type": "integer",
                "description": "Number of parlay suggestions to return (1-5, default 3).",
            },
        },
        "required": [],
    },
}

# Per-channel conversation history (keeps last few messages for context)
_chat_history = {}
_MAX_HISTORY = 20


def _get_gemini_model():
    """Lazily initialize the Gemini model with function calling tools."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        return genai.GenerativeModel(
            "gemini-2.5-flash",
            system_instruction=SYSTEM_PROMPT,
            tools=[{"function_declarations": [
                _SCRAPE_TOOL_DECLARATION,
                _PARLAY_TOOL_DECLARATION,
            ]}],
        )
    except Exception as e:
        log.error("Failed to initialize Gemini: %s", e)
        return None


def _format_bets_for_ai(bets):
    """Format scraped bets as a concise text summary for Gemini to present."""
    if not bets:
        return "No +EV bets found matching the filters."

    sorted_bets = sorted(bets, key=_ev_sort_key, reverse=True)
    lines = [f"Found {len(bets)} +EV bets:\n"]

    for i, bet in enumerate(sorted_bets[:25]):
        ev = bet.get("ev_pct", "?").replace("%", "").strip()
        odds = bet.get("odds", "?")
        fair = bet.get("fair_odds", "")
        book = bet.get("sportsbook", "?")
        event = bet.get("event", "?")
        pick = bet.get("bet_name", "?")
        market = bet.get("market", "")
        sport = bet.get("sport_league", "")

        line = f"{i+1}. [{book}] {event} — {pick}"
        if market:
            line += f" ({market})"
        line += f" | Odds: {odds}"
        if fair:
            line += f" / Fair: {fair}"
        line += f" | EV: {ev}%"
        if sport:
            line += f" | {sport}"
        lines.append(line)

    if len(bets) > 25:
        lines.append(f"\n...and {len(bets) - 25} more. Full list available via CSV.")

    return "\n".join(lines)


async def _handle_scrape_function_call(func_call):
    """Execute the scrape based on Gemini's function call and return results."""
    args = dict(func_call.args) if func_call.args else {}
    log.info("AI triggered scrape with args: %s", args)

    sportsbooks = args.get("sportsbooks", []) or None
    min_ev = args.get("min_ev", DEFAULT_MIN_EV)
    max_odds = args.get("max_odds", 400)   # wider than scheduled posts — include high-odds props
    min_odds = args.get("min_odds", DEFAULT_MIN_ODDS)

    try:
        bets, csv_path = await run_scrape_async(
            sportsbooks=sportsbooks,
            min_ev=min_ev,
            max_odds=max_odds,
            min_odds=min_odds,
            mainlines_only=False,   # AI queries should show props too
        )
        return bets, csv_path, _format_bets_for_ai(bets), []
    except Exception as e:
        log.exception("AI-triggered scrape failed")
        return [], None, f"Scrape failed: {e}", []


async def _handle_parlay_function_call(func_call):
    """Build parlays based on Gemini's function call."""
    args = dict(func_call.args) if func_call.args else {}
    log.info("AI triggered parlay build with args: %s", args)

    num_legs = int(args.get("num_legs", 3))
    sport = args.get("sport")
    sportsbook = args.get("sportsbook")
    same_game = args.get("same_game", False)
    max_parlays = min(int(args.get("max_parlays", 3)), 5)

    try:
        bets, csv_path = await run_scrape_async(mainlines_only=False, max_odds=400)
    except Exception as e:
        log.exception("Scrape for parlay failed")
        return [], f"Scrape failed: {e}", []

    if not bets:
        return [], "No +EV bets found — can't build parlays.", []

    parlays = build_parlays(
        bets, num_legs=num_legs, max_parlays=max_parlays,
        sport=sport, sportsbook=sportsbook, same_game=same_game,
    )

    if not parlays:
        return bets, f"Found {len(bets)} bets but couldn't build {num_legs}-leg parlays with those filters.", []

    # Format for AI text response
    lines = [f"Built {len(parlays)} parlay(s) from {len(bets)} +EV bets:\n"]
    for i, p in enumerate(parlays, 1):
        stats = p["stats"]
        lines.append(f"**Parlay #{i}** — {stats['american_odds']} ({stats['parlay_decimal']:.1f}x) | Combined EV: {stats['combined_ev']:.1f}% | $10 pays ${stats['payout_per_dollar'] * 10:.2f}")
        for j, leg in enumerate(p["legs"], 1):
            lines.append(f"  Leg {j}: {leg.get('bet_name', '?')} | {leg.get('event', '?')} | {leg.get('odds', '?')} | {leg.get('sportsbook', '?')}")
        lines.append("")

    return bets, "\n".join(lines), parlays


async def _ask_gemini(channel_id, user_name, question):
    """Send a message to Gemini with conversation history and function calling."""
    model = _get_gemini_model()
    if not model:
        return "Gemini AI is not configured. Set `GEMINI_API_KEY` env var.", [], None, []

    # Build conversation history
    if channel_id not in _chat_history:
        _chat_history[channel_id] = []

    history = _chat_history[channel_id]
    history.append({"role": "user", "parts": [f"{user_name}: {question}"]})

    # Trim to max history
    if len(history) > _MAX_HISTORY:
        history = history[-_MAX_HISTORY:]
        _chat_history[channel_id] = history

    loop = asyncio.get_event_loop()

    async def _send_with_retry(chat, message, max_retries=3):
        """Send a message to Gemini, retrying on 429 rate-limit errors."""
        for attempt in range(max_retries + 1):
            try:
                return await loop.run_in_executor(
                    None,
                    lambda: chat.send_message(message),
                )
            except Exception as e:
                err_str = str(e)
                if "429" in err_str and attempt < max_retries:
                    # Extract retry delay from error if available
                    match = re.search(r"retry in (\d+(?:\.\d+)?)", err_str, re.IGNORECASE)
                    wait = float(match.group(1)) + 1 if match else (2 ** attempt) * 5
                    wait = min(wait, 60)  # cap at 60s
                    log.info("Gemini rate limited, retrying in %.0fs (attempt %d/%d)",
                             wait, attempt + 1, max_retries)
                    await asyncio.sleep(wait)
                else:
                    raise

    try:
        chat = model.start_chat(history=history[:-1])
        response = await _send_with_retry(chat, history[-1]["parts"][0])

        # Check if Gemini wants to call a function
        candidate = response.candidates[0]
        bets = []
        csv_path = None
        parlays = []

        if candidate.content.parts:
            for part in candidate.content.parts:
                if not hasattr(part, "function_call"):
                    continue
                func_name = part.function_call.name
                import google.generativeai as genai

                if func_name == "scrape_ev_bets":
                    bets, csv_path, result_text, _ = await _handle_scrape_function_call(
                        part.function_call
                    )
                    func_response = genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name="scrape_ev_bets",
                            response={"result": result_text},
                        )
                    )
                    response = await _send_with_retry(chat, func_response)
                    break

                elif func_name == "build_parlay":
                    bets, result_text, parlays = await _handle_parlay_function_call(
                        part.function_call
                    )
                    func_response = genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name="build_parlay",
                            response={"result": result_text},
                        )
                    )
                    response = await _send_with_retry(chat, func_response)
                    break

        reply = response.text

        # Save assistant response to history
        history.append({"role": "model", "parts": [reply]})
        _chat_history[channel_id] = history

        return reply, bets, csv_path, parlays
    except Exception as e:
        log.exception("Gemini API error")
        err_str = str(e)
        if "429" in err_str:
            return "Rate limited by Gemini free tier. Please wait a minute and try again.", [], None, []
        return f"Gemini error: {e}", [], None, []


# ---------------------------------------------------------------------------
# Local intent detection fallback (no AI needed)
# ---------------------------------------------------------------------------

# Patterns that indicate the user wants to see bets
_BET_REQUEST_PATTERNS = re.compile(
    r"\b(show|get|find|give|check|what|any|pull|fetch|grab|list|see)"
    r".*\b(bets?|ev\b|odds|lines?|picks?|opportunities|plays?)\b",
    re.IGNORECASE,
)

# Sportsbook name variants → canonical names
_BOOK_ALIASES = {
    "fanduel": "FanDuel", "fd": "FanDuel",
    "draftkings": "DraftKings", "dk": "DraftKings",
    "betmgm": "BetMGM", "mgm": "BetMGM",
    "caesars": "Caesars", "czr": "Caesars",
    "betrivers": "BetRivers", "br": "BetRivers", "bet rivers": "BetRivers",
    "fanatics": "Fanatics", "fan": "Fanatics",
    "hard rock": "Hard Rock", "hardrock": "Hard Rock", "hr": "Hard Rock",
}


_PARLAY_REQUEST_PATTERNS = re.compile(
    r"\b(build|make|create|suggest|give|show|get)"
    r".*\b(parlays?|combo|combos|multi|accumulator)\b",
    re.IGNORECASE,
)


def _detect_bet_request(text):
    """Check if the message is asking for bets. Returns (is_bet_request, sportsbooks).
    Works without Gemini — pure regex/keyword matching."""
    if not _BET_REQUEST_PATTERNS.search(text):
        return False, []

    # Extract sportsbook names
    text_lower = text.lower()
    found_books = []
    for alias, canonical in _BOOK_ALIASES.items():
        if alias in text_lower and canonical not in found_books:
            found_books.append(canonical)

    return True, found_books


def _detect_parlay_request(text):
    """Check if the message is asking to build a parlay."""
    return bool(_PARLAY_REQUEST_PATTERNS.search(text))


async def _handle_ai_response(channel, channel_id, user_name, question):
    """Common handler for AI responses — sends reply, embeds, and CSV."""
    async with channel.typing():
        reply, bets, csv_path, parlays = await _ask_gemini(channel_id, user_name, question)

    # If Gemini failed with rate limit but the user was asking for bets,
    # fall back to running the scraper directly
    if not bets and not parlays and reply and ("rate limited" in reply.lower() or "429" in reply or "gemini error" in reply.lower()):
        is_bet_req, detected_books = _detect_bet_request(question)
        is_parlay_req = _detect_parlay_request(question)
        if is_bet_req or is_parlay_req:
            fallback_msg = await channel.send(
                "AI is rate-limited, but I detected a bet request — scraping directly..."
            )
            try:
                bets, csv_path = await run_scrape_async(
                    sportsbooks=detected_books or None,
                )
                if is_parlay_req:
                    parlays = build_parlays(bets)
                    await fallback_msg.edit(
                        content=f"\u2705 Built parlays from {len(bets)} +EV bets!"
                    )
                else:
                    await fallback_msg.edit(
                        content=f"\u2705 Found **{len(bets)}** +EV bets!"
                        + (f" (filtered to {', '.join(detected_books)})" if detected_books else "")
                    )
            except Exception as e:
                await fallback_msg.edit(content=f"Scrape failed: {e}")
                return
            # Don't send the error reply since we handled it
            reply = None

    # Send the AI's text reply
    if reply:
        while reply:
            await channel.send(reply[:1900])
            reply = reply[1900:]

    # If parlays were built, send parlay embeds
    if parlays:
        embeds = format_parlay_embeds(parlays)
        for i in range(0, len(embeds), 10):
            await channel.send(embeds=embeds[i : i + 10])
    # Otherwise if a scrape ran, send bet embeds
    elif bets:
        embeds = format_bet_embeds(bets)
        for i in range(0, len(embeds), 10):
            await channel.send(embeds=embeds[i : i + 10])



@bot.command(name="ask")
async def ask_command(ctx, *, question: str = ""):
    """Ask the AI a question about sports betting or EV strategy."""
    if not GEMINI_API_KEY:
        await ctx.send(
            "AI chat is not configured. The bot owner needs to set "
            "`GEMINI_API_KEY` environment variable."
        )
        return

    if not question.strip():
        await ctx.send("Usage: `!ask <your question>`\nExample: `!ask what is Kelly criterion?`")
        return

    await _handle_ai_response(
        ctx.channel,
        str(ctx.channel.id),
        ctx.author.display_name,
        question,
    )


@bot.command(name="clearchat")
async def clearchat_command(ctx):
    """Clear the AI conversation history for this channel."""
    channel_id = str(ctx.channel.id)
    if channel_id in _chat_history:
        del _chat_history[channel_id]
    await ctx.send("Chat history cleared.")


@bot.event
async def on_message(message):
    """Respond to @mentions with Gemini AI."""
    # Don't respond to ourselves
    if message.author == bot.user:
        return

    # Process commands first
    await bot.process_commands(message)

    # Respond to @mentions (but not command messages)
    if bot.user in message.mentions and not message.content.startswith("!"):
        if not GEMINI_API_KEY:
            await message.channel.send(
                "AI chat is not configured. Use `!ask` once `GEMINI_API_KEY` is set."
            )
            return

        # Strip the mention from the message
        question = message.content
        for mention in message.mentions:
            question = question.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
        question = question.strip()

        if not question:
            await message.channel.send("Hey! Ask me anything about sports betting or EV strategy.")
            return

        await _handle_ai_response(
            message.channel,
            str(message.channel.id),
            message.author.display_name,
            question,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        print(
            "Set DISCORD_TOKEN environment variable.\n"
            "  export DISCORD_TOKEN=your_bot_token_here\n"
            "  python discord_bot.py"
        )
        raise SystemExit(1)

    bot.run(TOKEN)
