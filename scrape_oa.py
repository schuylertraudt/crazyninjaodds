#!/usr/bin/env python3
"""
OddsAssist Pro +EV Scraper
Scrapes the Positive EV betting tool from pro.oddsassist.com using Playwright.
Requires login — set OA_EMAIL and OA_PASSWORD environment variables.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("oa-ev")

URL = "https://pro.oddsassist.com/advantages/plus-ev"
LOGIN_URL = "https://pro.oddsassist.com"

DEFAULT_SPORTSBOOKS = [
    "FanDuel",
    "DraftKings",
    "BetMGM",
    "Caesars",
    "BetRivers",
    "Fanatics",
    "Hard Rock",
    "Bet365",
    "theScore Bet",
]
DEFAULT_MIN_EV = 1.0
DEFAULT_MAX_ODDS = 250
DEFAULT_MIN_ODDS = -200
# Persistent browser profile directory — stores cookies/session after manual login
BROWSER_PROFILE_DIR = os.environ.get(
    "OA_BROWSER_PROFILE", os.path.join(os.path.expanduser("~"), ".oa_browser_profile")
)

CANONICAL_FIELDS = [
    "source",
    "sport_league",
    "event",
    "game_time",
    "market",
    "bet_name",
    "sportsbook",
    "odds",
    "fair_odds",
    "ev_pct",
]

# Map sportsbook icon alt text / labels to canonical names
BOOK_ICON_MAP = {
    "fanduel": "FanDuel",
    "draftkings": "DraftKings",
    "betmgm": "BetMGM",
    "caesars": "Caesars",
    "betrivers": "BetRivers",
    "fanatics": "Fanatics",
    "hard rock": "Hard Rock",
    "hardrock": "Hard Rock",
    "hard rock bet": "Hard Rock",
    "bet365": "Bet365",
    "pointsbet": "PointsBet",
    "espn bet": "ESPN BET",
    "espnbet": "ESPN BET",
    "fliff": "Fliff",
}


def _clean_odds(val):
    """Strip extra whitespace and parenthetical info from odds strings."""
    if not val:
        return val
    return re.sub(r"\s*\(.*?\)\s*$", "", val).strip()


def _parse_odds_int(val):
    """Parse odds string to int for filtering."""
    cleaned = _clean_odds(val or "").replace("+", "").strip()
    try:
        return int(cleaned)
    except (ValueError, TypeError):
        return None


def _identify_sportsbook(card):
    """Try to identify the sportsbook from a bet card element."""
    # Look for img alt text
    imgs = card.locator("img")
    for i in range(imgs.count()):
        alt = imgs.nth(i).get_attribute("alt") or ""
        alt_lower = alt.lower().strip()
        for key, name in BOOK_ICON_MAP.items():
            if key in alt_lower:
                return name

    # Look for aria-label on buttons/links
    links = card.locator("a, button")
    for i in range(links.count()):
        aria = links.nth(i).get_attribute("aria-label") or ""
        title = links.nth(i).get_attribute("title") or ""
        for text in [aria, title]:
            text_lower = text.lower()
            for key, name in BOOK_ICON_MAP.items():
                if key in text_lower:
                    return name

    # Look for sportsbook name in any text content
    full_text = card.inner_text().lower()
    for key, name in BOOK_ICON_MAP.items():
        if key in full_text:
            return name

    return "Unknown"


def oa_login(chromium_path=None):
    """Open a visible browser so the user can log in to OddsAssist Pro via Google OAuth.
    The session is saved to BROWSER_PROFILE_DIR for reuse by headless scrapes.

    Run this once:  python scrape_oa.py --login
    """
    log.info("Opening browser for manual login...")
    log.info("Browser profile will be saved to: %s", BROWSER_PROFILE_DIR)

    with sync_playwright() as pw:
        launch_kwargs = {"headless": False}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        context = pw.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            **launch_kwargs,
        )
        page = context.new_page()
        page.goto(URL, wait_until="networkidle", timeout=45_000)

        log.info("")
        log.info("=" * 60)
        log.info("  A browser window has opened.")
        log.info("  Please log in to OddsAssist Pro via Google OAuth.")
        log.info("  Once you see the +EV bets page, close the browser.")
        log.info("=" * 60)
        log.info("")

        # Wait for the user to close the browser
        try:
            page.wait_for_event("close", timeout=300_000)  # 5 min
        except Exception:
            pass

        context.close()
        log.info("Login session saved! You can now run headless scrapes.")


def _parse_bet_cards(page):
    """Parse the card-based +EV bet layout on OddsAssist Pro.

    Each bet appears as a card with:
    - Event header: team names, league, date/time, ROI%
    - Bet details: market type, pick, no vig odds, actual odds, sportsbook icon
    """
    bets = []

    # Wait for bet cards to load — try multiple selectors
    card_selectors = [
        # Common React component patterns
        "[class*='advantage']",
        "[class*='Advantage']",
        "[class*='bet-card']",
        "[class*='BetCard']",
        "[class*='ev-card']",
        "[class*='opportunity']",
        "[data-testid*='bet']",
        "[data-testid*='advantage']",
        # Broader selectors — look for the card structure we saw in screenshot
        "article",
        "[class*='card']",
    ]

    cards = None
    for sel in card_selectors:
        try:
            page.wait_for_selector(sel, timeout=5000)
            loc = page.locator(sel)
            count = loc.count()
            if count >= 1:
                log.info("Found %d elements with selector: %s", count, sel)
                cards = loc
                break
        except PwTimeout:
            continue

    if not cards or cards.count() == 0:
        # Fallback: try to parse directly from page text structure
        log.info("No card selectors matched, trying text-based parsing...")
        return _parse_from_text(page)

    for i in range(cards.count()):
        try:
            card = cards.nth(i)
            text = card.inner_text()
            if not text.strip():
                continue

            bet = _extract_bet_from_card_text(text, card)
            if bet and bet.get("event"):
                bet["source"] = "OddsAssist"
                bets.append(bet)
        except Exception as e:
            log.debug("Error parsing card %d: %s", i, e)
            continue

    return bets


def _extract_bet_from_card_text(text, card_element=None):
    """Extract bet data from the text content of a card element.

    Expected text structure from screenshot:
        Oklahoma City Thunder vs Los Angeles Lakers
        NBA - 4/7 10:30 PM
        0.8%
        ROI
        Moneyline - Full Match
        No Vig Odds
        +1090
        +1100
        Bet
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 4:
        return None

    bet = {}

    # Look for ROI% pattern (like "0.8%" or "1.5%")
    roi_pattern = re.compile(r"^(\d+\.?\d*)\s*%$")
    # Look for league + date pattern (like "NBA - 4/7 10:30 PM")
    league_date_pattern = re.compile(
        r"^([A-Z]{2,10})\s*-\s*(\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\s*(?:AM|PM)?)",
        re.IGNORECASE,
    )
    # Look for odds pattern
    odds_pattern = re.compile(r"^[+-]\d{3,4}$")
    # Look for "No Vig Odds" label
    no_vig_pattern = re.compile(r"no\s*vig\s*odds", re.IGNORECASE)

    # Parse structured data from lines
    event_line = None
    league = None
    game_time = None
    roi = None
    market = None
    bet_name = None
    no_vig_odds = None
    bet_odds = None

    i = 0
    while i < len(lines):
        line = lines[i]

        # Check for ROI%
        roi_match = roi_pattern.match(line)
        if roi_match:
            roi = roi_match.group(1)
            i += 1
            # Skip "ROI" label if next
            if i < len(lines) and lines[i].upper() == "ROI":
                i += 1
            continue

        # Check for league + date
        ld_match = league_date_pattern.match(line)
        if ld_match:
            league = ld_match.group(1)
            game_time = ld_match.group(2)
            i += 1
            continue

        # Check for "No Vig Odds" label
        if no_vig_pattern.search(line):
            # Next line should be the no-vig odds value
            if i + 1 < len(lines) and odds_pattern.match(lines[i + 1]):
                no_vig_odds = lines[i + 1]
                i += 2
                continue
            i += 1
            continue

        # Check for odds values
        if odds_pattern.match(line):
            if no_vig_odds is None:
                # This might be the no-vig odds (context dependent)
                no_vig_odds = line
            else:
                bet_odds = line
            i += 1
            continue

        # Check for "Bet" button text
        if line.lower() == "bet":
            i += 1
            continue

        # Check for "vs" in line — likely event name
        if " vs " in line.lower() or " @ " in line.lower():
            event_line = line
            i += 1
            continue

        # If we haven't found event yet, and this looks like a team matchup
        if event_line is None and not roi and not market:
            # Could be the event name (first substantial line)
            if len(line) > 5 and not line.startswith("+") and not line.startswith("-"):
                event_line = line
                i += 1
                continue

        # Market type (e.g., "Moneyline - Full Match", "Spread", "Total")
        if market is None and roi is not None:
            # After ROI, the next non-odds text is likely the market
            if not odds_pattern.match(line) and line.upper() != "ROI":
                market = line
                i += 1
                continue

        # Bet name / pick (e.g., "Los Angeles Lakers ML")
        if market is not None and bet_name is None:
            if not odds_pattern.match(line) and not no_vig_pattern.search(line):
                bet_name = line
                i += 1
                continue

        i += 1

    # Build the bet dict
    bet = {
        "event": event_line or "",
        "sport_league": league or "",
        "game_time": game_time or "",
        "market": market or "",
        "bet_name": bet_name or "",
        "odds": _clean_odds(bet_odds) or _clean_odds(no_vig_odds) or "",
        "fair_odds": _clean_odds(no_vig_odds) if bet_odds else "",
        "ev_pct": roi or "",
    }

    # Try to identify sportsbook from card element
    if card_element:
        bet["sportsbook"] = _identify_sportsbook(card_element)
    else:
        bet["sportsbook"] = ""

    return bet


def _parse_from_text(page):
    """Fallback parser — try to extract bets from the full page text."""
    log.info("Attempting text-based page parsing...")
    bets = []

    # Get full page text and try to identify bet blocks
    body_text = page.locator("body").inner_text()
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    # Look for patterns of: event, league-date, ROI%, market, pick, odds
    vs_pattern = re.compile(r".+\bvs?\b.+", re.IGNORECASE)
    roi_pattern = re.compile(r"^(\d+\.?\d*)\s*%$")

    current_block = []
    for line in lines:
        if vs_pattern.match(line) and current_block:
            # Start of a new bet — process the previous block
            bet = _extract_bet_from_card_text("\n".join(current_block))
            if bet and bet.get("event"):
                bet["source"] = "OddsAssist"
                bets.append(bet)
            current_block = [line]
        else:
            current_block.append(line)

    # Process last block
    if current_block:
        bet = _extract_bet_from_card_text("\n".join(current_block))
        if bet and bet.get("event"):
            bet["source"] = "OddsAssist"
            bets.append(bet)

    return bets


def write_csv(bets, output_dir):
    """Write bets to a timestamped CSV file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"oa_ev_bets_{ts}.csv"

    fieldnames = CANONICAL_FIELDS[:]
    for bet in bets:
        for k in bet:
            if k not in fieldnames:
                fieldnames.append(k)

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(bets)

    log.info("CSV written to %s (%d rows)", path, len(bets))
    return path


def scrape_oa(
    sportsbooks=None,
    min_ev=DEFAULT_MIN_EV,
    max_odds=DEFAULT_MAX_ODDS,
    min_odds=DEFAULT_MIN_ODDS,
    headless=True,
    chromium_path=None,
    output_dir=Path("."),
):
    """Core scraper function for OddsAssist Pro.
    Uses persistent browser profile for auth (run --login first).
    Returns (bets, csv_path). Raises RuntimeError on failure."""

    if sportsbooks is None:
        sportsbooks = DEFAULT_SPORTSBOOKS

    if not os.path.isdir(BROWSER_PROFILE_DIR):
        raise RuntimeError(
            "No saved browser session found. Run `python scrape_oa.py --login` first "
            "to log in via Google OAuth and save your session."
        )

    with sync_playwright() as pw:
        launch_kwargs = {"headless": headless}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        log.info("Launching browser with saved session (headless=%s) …", headless)
        context = pw.chromium.launch_persistent_context(
            BROWSER_PROFILE_DIR,
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            **launch_kwargs,
        )
        page = context.new_page()

        # Navigate to the +EV page
        log.info("Navigating to %s …", URL)
        try:
            page.goto(URL, wait_until="networkidle", timeout=45_000)
        except PwTimeout:
            log.warning("networkidle timeout — continuing anyway")

        log.info("Page loaded: %s (URL: %s)", page.title() or "(no title)", page.url)

        # Check if session expired (redirected to login)
        current_url = page.url.lower()
        if "login" in current_url or "signin" in current_url or "sign-in" in current_url:
            context.close()
            raise RuntimeError(
                "Session expired — run `python scrape_oa.py --login` again to re-authenticate."
            )

        # Wait for content to load
        log.info("Waiting for +EV bets to load...")
        page.wait_for_timeout(3000)

        # Try to wait for dynamic content
        try:
            page.wait_for_selector(
                "[class*='advantage'], [class*='card'], article, [class*='bet']",
                timeout=15_000,
            )
        except PwTimeout:
            log.warning("Card elements not found within timeout")

        # Additional wait for React hydration
        page.wait_for_timeout(2000)

        # Parse the bet cards
        log.info("Parsing +EV bet cards...")
        bets = _parse_bet_cards(page)
        log.info("Parsed %d raw bets", len(bets))

        if not bets:
            log.error("No bets found on OddsAssist Pro!")
            try:
                page.screenshot(path="debug_oa_screenshot.png", timeout=10_000)
                log.info("Screenshot saved to debug_oa_screenshot.png")
            except Exception as e:
                log.warning("Could not save screenshot: %s", e)

            with open("debug_oa_page.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            log.info("Page HTML saved to debug_oa_page.html")

            context.close()
            raise RuntimeError("No bets found on OddsAssist Pro — check debug_oa_page.html")

        if bets:
            log.info("Sample OA bet: %s", bets[0])

        # Client-side sportsbook filter
        sb_lower = {s.lower() for s in sportsbooks}
        before = len(bets)
        bets = [
            b for b in bets
            if not b.get("sportsbook")
            or b["sportsbook"].lower() in sb_lower
            or b["sportsbook"] == "Unknown"
        ]
        if len(bets) < before:
            log.info("Filtered sportsbooks: %d → %d rows", before, len(bets))

        # Client-side EV% filter
        before = len(bets)
        filtered = []
        for b in bets:
            ev_str = b.get("ev_pct", "").replace("%", "").replace("+", "").strip()
            try:
                if float(ev_str) >= min_ev:
                    filtered.append(b)
            except ValueError:
                filtered.append(b)
        bets = filtered
        if len(bets) < before:
            log.info("Filtered min EV%%: %d → %d rows", before, len(bets))

        # Client-side odds range filter
        before = len(bets)
        filtered = []
        for b in bets:
            odds_val = _parse_odds_int(b.get("odds", ""))
            if odds_val is None:
                filtered.append(b)
            elif min_odds <= odds_val <= max_odds:
                filtered.append(b)
        bets = filtered
        if len(bets) < before:
            log.info("Filtered odds range: %d → %d rows", before, len(bets))

        # Write CSV
        csv_path = write_csv(bets, output_dir) if bets else None

        context.close()
        log.info("Done — %d OddsAssist bets found.", len(bets))
        return bets, csv_path


def parse_args():
    p = argparse.ArgumentParser(description="Scrape +EV bets from OddsAssist Pro")
    p.add_argument("--login", action="store_true",
                    help="Open a browser to log in via Google OAuth (run once)")
    p.add_argument("--sportsbooks", nargs="+", default=DEFAULT_SPORTSBOOKS)
    p.add_argument("--min-ev", type=float, default=DEFAULT_MIN_EV)
    p.add_argument("--max-odds", type=int, default=DEFAULT_MAX_ODDS)
    p.add_argument("--min-odds", type=int, default=DEFAULT_MIN_ODDS)
    p.add_argument("--output-dir", type=Path, default=Path("."))
    p.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--chromium-path", type=str, default=None)
    return p.parse_args()


def main():
    args = parse_args()

    if args.login:
        oa_login(chromium_path=args.chromium_path)
        return

    try:
        bets, csv_path = scrape_oa(
            sportsbooks=args.sportsbooks,
            min_ev=args.min_ev,
            max_odds=args.max_odds,
            min_odds=args.min_odds,
            headless=args.headless,
            chromium_path=args.chromium_path,
            output_dir=args.output_dir,
        )
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)

    if not bets:
        log.warning("No bets remaining after filters!")
        sys.exit(0)

    # Print table
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="OddsAssist +EV Bets", show_lines=True)
        display_cols = [c for c in CANONICAL_FIELDS if any(c in b for b in bets)]
        for col in display_cols:
            table.add_column(col.replace("_", " ").title(), overflow="fold")
        for bet in bets:
            row = [str(bet.get(col, "")) for col in display_cols]
            table.add_row(*row)
        console.print(table)
    except ImportError:
        for b in bets:
            print(b)

    log.info("Done.")


if __name__ == "__main__":
    main()
