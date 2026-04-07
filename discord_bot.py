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
import json
import logging
import os
import re
import shlex
import time
from datetime import datetime
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
            time = bet.get("game_time", "").strip()

            # Field name: event
            title = event
            if sport:
                title = f"{sport} \u2022 {event}"

            # Field value: simple, no backticks or complex nesting
            ev_display = str(ev).replace("%", "").replace("+", "").strip()
            lines = [
                f"\u27A1 **{pick}**" + (f" ({market})" if market else ""),
                f"\U0001f4b2 Odds: **{odds}**" + (f" | Fair: **{fair}**" if fair else ""),
                f"\U0001f4c8 EV: **{ev_display}%**",
                f"\U0001f3e6 {book}" + (f" | {time}" if time else ""),
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
            text=f"Showing top {total_shown} of {len(bets)} bets \u2022 Full list in CSV"
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
            max_odds=max_odds,
            min_odds=min_odds,
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
    "Extract sportsbook names and any filters from the user's message. "
    "Available sportsbooks: FanDuel, DraftKings, BetMGM, Caesars, BetRivers, Fanatics."
)

# Gemini function declaration for the scraper tool
_SCRAPE_TOOL_DECLARATION = {
    "name": "scrape_ev_bets",
    "description": (
        "Scrape live +EV (positive expected value) sports bets from CrazyNinjaOdds. "
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
                    "BetMGM, Caesars, BetRivers, Fanatics. "
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

# Per-channel conversation history (keeps last few messages for context)
_chat_history = {}
_MAX_HISTORY = 20


def _get_gemini_model():
    """Lazily initialize the Gemini model with function calling tools."""
    try:
        import google.generativeai as genai
        genai.configure(api_key=GEMINI_API_KEY)
        return genai.GenerativeModel(
            "gemini-2.0-flash",
            system_instruction=SYSTEM_PROMPT,
            tools=[{"function_declarations": [_SCRAPE_TOOL_DECLARATION]}],
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
    max_odds = args.get("max_odds", DEFAULT_MAX_ODDS)
    min_odds = args.get("min_odds", DEFAULT_MIN_ODDS)

    try:
        bets, csv_path = await run_scrape_async(
            sportsbooks=sportsbooks,
            min_ev=min_ev,
            max_odds=max_odds,
            min_odds=min_odds,
        )
        return bets, csv_path, _format_bets_for_ai(bets)
    except Exception as e:
        log.exception("AI-triggered scrape failed")
        return [], None, f"Scrape failed: {e}"


async def _ask_gemini(channel_id, user_name, question):
    """Send a message to Gemini with conversation history and function calling."""
    model = _get_gemini_model()
    if not model:
        return "Gemini AI is not configured. Set `GEMINI_API_KEY` env var.", [], None

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

        # Check if Gemini wants to call our scrape function
        candidate = response.candidates[0]
        bets = []
        csv_path = None

        if candidate.content.parts:
            for part in candidate.content.parts:
                if hasattr(part, "function_call") and part.function_call.name == "scrape_ev_bets":
                    bets, csv_path, result_text = await _handle_scrape_function_call(
                        part.function_call
                    )

                    # Send the function result back to Gemini so it can summarize
                    import google.generativeai as genai
                    func_response = genai.protos.Part(
                        function_response=genai.protos.FunctionResponse(
                            name="scrape_ev_bets",
                            response={"result": result_text},
                        )
                    )
                    response = await _send_with_retry(chat, func_response)
                    break

        reply = response.text

        # Save assistant response to history
        history.append({"role": "model", "parts": [reply]})
        _chat_history[channel_id] = history

        return reply, bets, csv_path
    except Exception as e:
        log.exception("Gemini API error")
        err_str = str(e)
        if "429" in err_str:
            return ("Rate limited by Gemini free tier. Please wait a minute and try again."), [], None
        return f"Gemini error: {e}", [], None


async def _handle_ai_response(channel, channel_id, user_name, question):
    """Common handler for AI responses — sends reply, embeds, and CSV."""
    async with channel.typing():
        reply, bets, csv_path = await _ask_gemini(channel_id, user_name, question)

    # Send the AI's text reply
    while reply:
        await channel.send(reply[:1900])
        reply = reply[1900:]

    # If the AI triggered a scrape, also send embeds and CSV
    if bets:
        embeds = format_bet_embeds(bets)
        for i in range(0, len(embeds), 10):
            await channel.send(embeds=embeds[i : i + 10])

        if csv_path and csv_path.exists():
            await channel.send(
                content="\U0001f4ce Full data attached:",
                file=discord.File(str(csv_path)),
            )


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
