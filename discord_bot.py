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
import io
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
    CANONICAL_FIELDS,
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


def format_discord_message(bets, max_bets=25):
    """Format bets into a Discord-friendly message string."""
    if not bets:
        return "No +EV bets found matching your filters."

    lines = [f"**+EV Bets Found: {len(bets)}**\n"]

    display_bets = bets[:max_bets]
    for i, bet in enumerate(display_bets, 1):
        event = bet.get("event", "—")
        book = bet.get("sportsbook", "—")
        market = bet.get("market", "")
        pick = bet.get("bet_name", "—")
        odds = bet.get("odds", "—")
        fair = bet.get("fair_odds", "")
        ev = bet.get("ev_pct", "—")
        kelly = bet.get("kelly", "")
        sport = bet.get("sport_league", "")

        line = f"`{i:>2}.` "
        if sport:
            line += f"**{sport}** | "
        line += f"{event}"
        if market:
            line += f" — {market}"
        line += f"\n     {pick} @ **{odds}**"
        if fair:
            line += f" (fair: {fair})"
        line += f" | EV: **{ev}**"
        if kelly:
            line += f" | Kelly: {kelly}"
        line += f" | {book}"
        lines.append(line)

    if len(bets) > max_bets:
        lines.append(f"\n*… and {len(bets) - max_bets} more (see CSV)*")

    return "\n".join(lines)


def format_embed(bets):
    """Create a summary embed."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    embed = discord.Embed(
        title="+EV Scrape Results",
        description=f"{len(bets)} bets found at {now}",
        color=0x00CC66 if bets else 0xFF4444,
    )

    if bets:
        # Top 5 by EV%
        sorted_bets = sorted(
            bets,
            key=lambda b: float(
                b.get("ev_pct", "0").replace("%", "").replace("+", "").strip() or "0"
            ),
            reverse=True,
        )
        top = sorted_bets[:5]
        top_lines = []
        for b in top:
            top_lines.append(
                f"{b.get('event', '?')} — {b.get('bet_name', '?')} @ {b.get('odds', '?')} "
                f"(**{b.get('ev_pct', '?')}** EV) [{b.get('sportsbook', '?')}]"
            )
        embed.add_field(
            name="Top 5 by EV%",
            value="\n".join(top_lines) or "—",
            inline=False,
        )

        # Sportsbook breakdown
        books = {}
        for b in bets:
            sb = b.get("sportsbook", "Unknown")
            books[sb] = books.get(sb, 0) + 1
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(books.items()))
        embed.add_field(name="By Sportsbook", value=breakdown, inline=False)

    embed.set_footer(text="CrazyNinjaOdds +EV Scraper")
    return embed


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

    # Post results
    await status_msg.edit(content=f"Found {len(bets)} +EV bets!")

    # Send embed summary
    embed = format_embed(bets)
    await ctx.send(embed=embed)

    # Send text listing
    msg_text = format_discord_message(bets)
    # Discord has a 2000 char limit per message
    for chunk in _chunk_message(msg_text, 1900):
        await ctx.send(chunk)

    # Attach CSV
    if csv_path and csv_path.exists():
        await ctx.send(file=discord.File(str(csv_path)))


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

    embed = format_embed(bets)
    await channel.send(embed=embed)

    msg_text = format_discord_message(bets)
    for chunk in _chunk_message(msg_text, 1900):
        await channel.send(chunk)

    if csv_path and csv_path.exists():
        await channel.send(file=discord.File(str(csv_path)))


def _chunk_message(text, max_len=1900):
    """Split a message into chunks that fit Discord's 2000-char limit."""
    lines = text.split("\n")
    chunks = []
    current = []
    current_len = 0

    for line in lines:
        if current_len + len(line) + 1 > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += len(line) + 1

    if current:
        chunks.append("\n".join(current))

    return chunks


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
