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
  !evstop          - Cancel a running scrape
  !evschedule 15   - Auto-post every 15 minutes
  !evschedule off  - Stop auto-posting
"""

import asyncio
import logging
import os
import shlex
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands, tasks

from scrape_ev import (
    DEFAULT_DEVIG,
    DEFAULT_MIN_BOOKS,
    DEFAULT_MIN_EV,
    DEFAULT_SPORTSBOOKS,
    scrape_ev,
)

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
AUTO_CHANNEL_ID = os.environ.get("CNO_CHANNEL_ID", "")
OUTPUT_DIR = Path("./csv_output")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Tracks the running scrape task so it can be cancelled
_scrape_task = None
_schedule_interval = None


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
    "fanatics": "FAN",
}


def _ev_sort_key(bet):
    """Parse EV% string to float for sorting."""
    raw = bet.get("ev_pct", "0").replace("%", "").replace("+", "").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


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
    now = datetime.now().strftime("%b %d, %I:%M %p")

    # --- Summary embed ---
    summary = discord.Embed(
        title=f"\U0001f4b0 {len(bets)} +EV Bets Found",
        description=f"Scraped {now}",
        color=0x00CC66,
    )

    # Sportsbook breakdown as inline fields
    books = {}
    for b in bets:
        sb = b.get("sportsbook", "Unknown")
        books[sb] = books.get(sb, 0) + 1
    for sb_name, count in sorted(books.items(), key=lambda x: -x[1]):
        summary.add_field(
            name=f"`{_book_badge(sb_name)}`",
            value=f"**{count}** bets",
            inline=True,
        )

    # EV range
    evs = [_ev_sort_key(b) for b in bets]
    if evs:
        summary.add_field(
            name="EV% Range",
            value=f"**{min(evs):.1f}%** — **{max(evs):.1f}%**",
            inline=False,
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
            color=0x2F3136,  # dark theme friendly
        )
        if total_pages > 1:
            bet_embed.set_author(name=f"Page {page_num}/{total_pages}")

        for bet in chunk:
            sport = bet.get("sport_league", "")
            event = bet.get("event", "—")
            market = bet.get("market", "")
            pick = bet.get("bet_name", "—")
            odds = bet.get("odds", "—")
            fair = bet.get("fair_odds", "")
            ev = bet.get("ev_pct", "—")
            kelly = bet.get("kelly", "")
            book = bet.get("sportsbook", "—")
            time = bet.get("game_time", "")

            # Field name: compact event + sport line
            name_parts = []
            if sport:
                name_parts.append(f"`{sport}`")
            name_parts.append(event)
            field_name = " \u2022 ".join(name_parts)

            # Field value: the bet details as a clean card
            lines = []
            lines.append(f"\u2022 **{pick}**" + (f" ({market})" if market else ""))
            lines.append(
                f"\u2022 Odds: `{odds}`"
                + (f"  Fair: `{fair}`" if fair else "")
            )

            ev_display = str(ev).replace("%", "").replace("+", "").strip()
            ev_line = f"\u2022 EV: **{ev_display}%** `{_ev_bar(ev)}`"
            if kelly:
                ev_line += f"  Kelly: **{kelly}**"
            lines.append(ev_line)

            badge = _book_badge(book)
            book_line = f"\u2022 `{badge}` {book}"
            if time:
                book_line += f"  \u23f0 {time}"
            lines.append(book_line)

            bet_embed.add_field(
                name=field_name,
                value="\n".join(lines),
                inline=False,
            )

        embeds.append(bet_embed)

    # Overflow note
    if len(bets) > total_shown:
        embeds[-1].set_footer(
            text=f"Showing top {total_shown} of {len(bets)} bets \u2022 Full list in CSV"
        )
    else:
        embeds[-1].set_footer(text="CrazyNinjaOdds +EV Scraper")

    return embeds


async def run_scrape_async(
    sportsbooks=None,
    min_ev=DEFAULT_MIN_EV,
    min_books=DEFAULT_MIN_BOOKS,
    devig_method=DEFAULT_DEVIG,
    mainlines_only=True,
):
    """Run the scraper in a thread pool so it doesn't block the bot."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        None,
        lambda: scrape_ev(
            sportsbooks=sportsbooks or DEFAULT_SPORTSBOOKS,
            min_ev=min_ev,
            mainlines_only=mainlines_only,
            devig_method=devig_method,
            min_books=min_books,
            headless=True,
            output_dir=OUTPUT_DIR,
        ),
    )


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------


@bot.event
async def on_ready():
    log.info("Bot ready: %s (id=%s)", bot.user, bot.user.id)
    if AUTO_CHANNEL_ID:
        log.info("Auto-post channel: %s", AUTO_CHANNEL_ID)


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
    devig = DEFAULT_DEVIG
    mainlines = True

    if raw_args.strip() == "--help":
        help_text = (
            "**!ev options:**\n"
            "`--min-ev <float>` — Minimum EV% (default: 1.0)\n"
            "`--min-books <int>` — Minimum sportsbooks with line (default: 3)\n"
            "`--sportsbooks <book1> <book2> ...` — Filter to specific books\n"
            "`--no-mainlines-only` — Include props\n"
            "\n**Examples:**\n"
            "`!ev` — defaults\n"
            "`!ev --min-ev 2.5 --sportsbooks FanDuel DraftKings`\n"
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
        f"Scraping +EV bets (min EV: {min_ev}%, books: {', '.join(sportsbooks)}) …"
    )

    try:
        bets, csv_path = await run_scrape_async(
            sportsbooks=sportsbooks,
            min_ev=min_ev,
            min_books=min_books,
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

    # Attach CSV
    if csv_path and csv_path.exists():
        await ctx.send(
            content="\U0001f4ce Full data attached:",
            file=discord.File(str(csv_path)),
        )


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


@tasks.loop(minutes=15)
async def _auto_post_loop():
    channel_id = getattr(_auto_post_loop, "_channel_id", None)
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if not channel:
        return

    log.info("Auto-post: running scheduled scrape …")
    try:
        bets, csv_path = await run_scrape_async()
    except Exception as e:
        log.exception("Scheduled scrape failed")
        await channel.send(f"Scheduled scrape failed: {e}")
        return

    if not bets:
        await channel.send("Scheduled scrape: no +EV bets found.")
        return

    embeds = format_bet_embeds(bets)
    for i in range(0, len(embeds), 10):
        await channel.send(embeds=embeds[i : i + 10])

    if csv_path and csv_path.exists():
        await channel.send(
            content="\U0001f4ce Full data attached:",
            file=discord.File(str(csv_path)),
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
