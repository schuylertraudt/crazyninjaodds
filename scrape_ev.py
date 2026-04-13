#!/usr/bin/env python3
"""
CNO +EV Scraper
Scrapes the Positive EV table from crazyninjaodds.com using Playwright.
"""

import argparse
import csv
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cno-ev")

URL = "https://crazyninjaodds.com/site/tools/positive-ev.aspx"
CNO_BASE_URL = "https://crazyninjaodds.com"

DEFAULT_SPORTSBOOKS = [
    "FanDuel",
    "DraftKings",
    "BetMGM",
    "Caesars",
    "BetRivers",
    "Fanatics",
]
DEFAULT_MIN_EV = 1.0
DEFAULT_MAINLINES_ONLY = True
DEFAULT_DEVIG = "Liquidity-Weighted Worst-case"
DEFAULT_MIN_BOOKS = 3
DEFAULT_MAX_ODDS = 250    # filter out odds > +250
DEFAULT_MIN_ODDS = -200   # filter out odds < -200
TABLE_TIMEOUT_MS = 30_000


def parse_args():
    p = argparse.ArgumentParser(description="Scrape +EV bets from CrazyNinjaOdds")
    p.add_argument(
        "--sportsbooks",
        nargs="+",
        default=DEFAULT_SPORTSBOOKS,
        help="Sportsbooks to include (default: %(default)s)",
    )
    p.add_argument(
        "--min-ev",
        type=float,
        default=DEFAULT_MIN_EV,
        help="Minimum EV%% (default: %(default)s)",
    )
    p.add_argument(
        "--mainlines-only",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_MAINLINES_ONLY,
        help="Mainlines only (default: %(default)s)",
    )
    p.add_argument(
        "--devig-method",
        default=DEFAULT_DEVIG,
        help="Devig method (default: %(default)s)",
    )
    p.add_argument(
        "--min-books",
        type=int,
        default=DEFAULT_MIN_BOOKS,
        help="Minimum number of books (default: %(default)s)",
    )
    p.add_argument(
        "--max-odds",
        type=int,
        default=DEFAULT_MAX_ODDS,
        help="Exclude bets with odds above this (e.g. 250 filters out +250 and higher, default: %(default)s)",
    )
    p.add_argument(
        "--min-odds",
        type=int,
        default=DEFAULT_MIN_ODDS,
        help="Exclude bets with odds below this (e.g. -200 filters out -200 and lower, default: %(default)s)",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        help="Directory for CSV output (default: current dir)",
    )
    p.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run browser in headless mode (default: True)",
    )
    p.add_argument(
        "--intercept-api",
        action="store_true",
        default=False,
        help="Intercept XHR/fetch requests and log API endpoints to api_debug.json",
    )
    p.add_argument(
        "--chromium-path",
        type=str,
        default=None,
        help="Path to Chromium executable (auto-detected if not set)",
    )
    return p.parse_args()


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------


def apply_filters(page, args):
    """Attempt to set filter controls on the page. Best-effort — logs warnings
    if controls aren't found (DOM may vary across site versions)."""
    log.info("Applying filters …")

    # --- Devig method dropdown ---
    try:
        devig_sel = page.locator(
            "select[id*='devig' i], select[id*='Devig' i], "
            "select[id*='method' i], select[id*='Method' i]"
        )
        if devig_sel.count() > 0:
            devig_sel.first.select_option(label=args.devig_method)
            log.info("  Devig method → %s", args.devig_method)
        else:
            log.warning("  Devig dropdown not found")
    except Exception as e:
        log.warning("  Could not set devig method: %s", e)

    # --- Min EV% ---
    try:
        ev_input = page.locator(
            "input[id*='minev' i], input[id*='MinEV' i], "
            "input[id*='ev' i][type='number'], input[id*='ev' i][type='text']"
        )
        if ev_input.count() > 0:
            ev_input.first.fill(str(args.min_ev))
            log.info("  Min EV%% → %s", args.min_ev)
        else:
            log.warning("  Min EV input not found")
    except Exception as e:
        log.warning("  Could not set min EV: %s", e)

    # --- Min books ---
    try:
        books_input = page.locator(
            "input[id*='minbook' i], input[id*='MinBook' i], "
            "input[id*='book' i][type='number']"
        )
        if books_input.count() > 0:
            books_input.first.fill(str(args.min_books))
            log.info("  Min books → %s", args.min_books)
        else:
            log.warning("  Min books input not found")
    except Exception as e:
        log.warning("  Could not set min books: %s", e)

    # --- Mainlines only checkbox ---
    try:
        ml_check = page.locator(
            "input[id*='mainline' i][type='checkbox'], "
            "input[id*='Mainline' i][type='checkbox']"
        )
        if ml_check.count() > 0:
            is_checked = ml_check.first.is_checked()
            if args.mainlines_only and not is_checked:
                ml_check.first.check()
                log.info("  Mainlines only → checked")
            elif not args.mainlines_only and is_checked:
                ml_check.first.uncheck()
                log.info("  Mainlines only → unchecked")
        else:
            log.warning("  Mainlines checkbox not found")
    except Exception as e:
        log.warning("  Could not set mainlines: %s", e)

    # Brief pause for filters to take effect
    page.wait_for_timeout(1000)

    # Try clicking an "Apply" / "Search" / "Filter" button if one exists
    try:
        apply_btn = page.locator(
            "button:has-text('Apply'), button:has-text('Search'), "
            "button:has-text('Filter'), input[type='submit'][value*='Apply' i], "
            "input[type='submit'][value*='Search' i], "
            "a:has-text('Apply'), a:has-text('Search')"
        )
        if apply_btn.count() > 0:
            apply_btn.first.click()
            log.info("  Clicked apply/search button")
            page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("  No apply button or click failed: %s", e)


# ---------------------------------------------------------------------------
# Table detection and parsing
# ---------------------------------------------------------------------------

# Ordered list of selector strategies to find the data table
TABLE_STRATEGIES = [
    # Standard HTML table
    {
        "name": "html-table",
        "wait": "table tbody tr td",
        "headers": "table thead th, table thead td",
        "rows": "table tbody tr",
        "cells": "td",
    },
    # AG Grid (common in modern .NET apps)
    {
        "name": "ag-grid",
        "wait": ".ag-row .ag-cell",
        "headers": ".ag-header-cell-text",
        "rows": ".ag-row",
        "cells": ".ag-cell",
    },
    # Div-based grid with role attributes
    {
        "name": "role-grid",
        "wait": "[role='gridcell']",
        "headers": "[role='columnheader']",
        "rows": "[role='row']",
        "cells": "[role='gridcell']",
    },
    # Kendo / Telerik grid
    {
        "name": "kendo-grid",
        "wait": ".k-grid-content tr td",
        "headers": ".k-grid-header th",
        "rows": ".k-grid-content tr",
        "cells": "td",
    },
    # DevExpress grid
    {
        "name": "dx-grid",
        "wait": ".dx-datagrid-rowsview tr td",
        "headers": ".dx-datagrid-headers td",
        "rows": ".dx-datagrid-rowsview tr",
        "cells": "td",
    },
    # Generic div table with class hints
    {
        "name": "div-table",
        "wait": "div[class*='table'] div[class*='row'] div[class*='cell']",
        "headers": "div[class*='table'] div[class*='header'] div[class*='cell']",
        "rows": "div[class*='table'] div[class*='row']:not([class*='header'])",
        "cells": "div[class*='cell']",
    },
]

# Known / expected column names and their canonical mapping
COLUMN_ALIASES = {
    "sport": "sport",
    "league": "league",
    "sport / league": "sport_league",
    "sport/league": "sport_league",
    "event": "event",
    "game": "event",
    "matchup": "event",
    "teams": "event",
    "event name": "event",
    "start": "game_time",
    "game time": "game_time",
    "start time": "game_time",
    "time": "game_time",
    "date": "game_time",
    "game date": "game_time",
    "market": "market",
    "market type": "market",
    "bet type": "market",
    "type": "market",
    "bet": "bet_name",
    "bet name": "bet_name",
    "selection": "bet_name",
    "pick": "bet_name",
    "wager": "bet_name",
    "outcome": "bet_name",
    "sportsbook": "sportsbook",
    "book": "sportsbook",
    "odds": "odds",
    "price": "odds",
    "american odds": "odds",
    "fair odds": "fair_odds",
    "fair value": "fair_odds",
    "fair": "fair_odds",
    "no-vig odds": "fair_odds",
    "no vig odds": "fair_odds",
    "true odds": "fair_odds",
    "ev": "ev_pct",
    "ev%": "ev_pct",
    "+ev": "ev_pct",
    "+ev%": "ev_pct",
    "ev percentage": "ev_pct",
    "expected value": "ev_pct",
    "lw-wc ev%": "ev_pct",
    "lw-wc ev": "ev_pct",
    "lw ev%": "ev_pct",
    "wc ev%": "ev_pct",
    "worst-case ev%": "ev_pct",
    "worst case ev%": "ev_pct",
    "kelly": "kelly",
    "kelly stake": "kelly",
    "kelly %": "kelly",
    "stake": "kelly",
    "edge": "ev_pct",
    "books": "books",
    "calc": "calc",
    "extra": "extra",
}

CANONICAL_FIELDS = [
    "sport_league",
    "event",
    "game_time",
    "market",
    "bet_name",
    "sportsbook",
    "odds",
    "fair_odds",
    "ev_pct",
    "kelly",
]


def _resolve_url(href):
    """Resolve a (possibly relative) href against the CNO base URL."""
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        return href
    if href.startswith("/"):
        return CNO_BASE_URL + href
    return href


def detect_table(page):
    """Try each table strategy and return (strategy_name, headers, rows_data, rows_links)."""
    for strat in TABLE_STRATEGIES:
        log.info("  Trying strategy: %s …", strat["name"])
        try:
            page.wait_for_selector(strat["wait"], timeout=5000)
        except PwTimeout:
            continue

        # Read headers — keep track of which column indices have text
        header_els = page.locator(strat["headers"])
        hcount = header_els.count()
        if hcount == 0:
            log.info("    No headers found, skipping")
            continue

        headers = []
        header_indices = []  # track original column indices
        for i in range(hcount):
            txt = header_els.nth(i).inner_text().strip()
            headers.append(txt)  # keep ALL headers including empty
            if txt:
                header_indices.append(i)

        non_empty_headers = [h for h in headers if h]
        if len(non_empty_headers) < 3:
            log.info("    Only %d non-empty headers, probably not the data table", len(non_empty_headers))
            continue

        log.info("    Total header elements: %d, non-empty: %d", hcount, len(non_empty_headers))

        if len(headers) < 3:
            log.info("    Only %d headers, probably not the data table", len(headers))
            continue

        # Read rows
        row_els = page.locator(strat["rows"])
        rcount = row_els.count()
        log.info("    Found %d headers, %d rows", len(headers), rcount)

        if rcount == 0:
            continue

        rows_data = []
        rows_links = []
        for i in range(rcount):
            row = row_els.nth(i)
            cell_els = row.locator(strat["cells"])
            ccount = cell_els.count()
            all_cells = []
            all_links = []
            for j in range(ccount):
                cell_el = cell_els.nth(j)
                all_cells.append(cell_el.inner_text().strip())
                try:
                    a = cell_el.locator("a[href]")
                    href = a.first.get_attribute("href") if a.count() > 0 else ""
                    all_links.append(href or "")
                except Exception:
                    all_links.append("")
            # Only keep cells at positions that correspond to non-empty headers
            # This handles hidden/empty columns that exist in the DOM but aren't real data
            if ccount == hcount and hcount != len(non_empty_headers):
                # Row has same cell count as total headers — pick only non-empty header positions
                cells = [all_cells[idx] for idx in header_indices if idx < ccount]
                links = [all_links[idx] for idx in header_indices if idx < ccount]
            else:
                cells = all_cells
                links = all_links
            if cells and any(c for c in cells):
                rows_data.append(cells)
                # First non-empty href in the row becomes the bet URL
                rows_links.append(next((h for h in links if h), ""))

        if rows_data:
            return strat["name"], non_empty_headers, rows_data, rows_links

    return None, [], [], []


def map_columns(raw_headers):
    """Map raw header names to canonical field names. Returns a list of
    canonical names (or the raw name if unrecognized)."""
    mapped = []
    for h in raw_headers:
        key = h.lower().strip().rstrip(":")
        canonical = COLUMN_ALIASES.get(key, key.replace(" ", "_"))
        mapped.append(canonical)
    return mapped


def _clean_odds(val):
    """Strip stake/unit info from odds like '+335 ($4)' → '+335'."""
    if not val:
        return val
    # Remove parenthetical like ($4), ($5.50)
    import re
    return re.sub(r"\s*\(.*?\)\s*$", "", val).strip()


def rows_to_dicts(headers, rows, row_links=None):
    """Convert list-of-lists into list-of-dicts using mapped headers."""
    mapped = map_columns(headers)
    log.info("Column mapping: %s", dict(zip(headers, mapped)))

    # Log first few rows for debugging alignment
    for idx, row in enumerate(rows[:3]):
        log.info("Raw row %d (%d cells): %s", idx, len(row), row)

    # Detect and handle column count mismatch — if rows consistently have
    # fewer cells than headers, the extra columns (Calc, Extra, etc.) may
    # be collapsed in the DOM. Drop unmapped/utility headers to realign.
    if rows:
        row_len = len(rows[0])
        if row_len < len(mapped):
            # Find which columns are utility/empty and can be dropped
            droppable = {"calc", "extra"}
            keep_indices = [i for i, col in enumerate(mapped) if col not in droppable]
            if len(keep_indices) == row_len:
                log.info(
                    "Row has %d cells but %d headers — dropping utility columns: %s",
                    row_len, len(mapped),
                    [h for i, h in enumerate(headers) if mapped[i] in droppable],
                )
                mapped = [mapped[i] for i in keep_indices]
            else:
                log.warning(
                    "Row has %d cells but %d headers — cannot auto-align!",
                    row_len, len(mapped),
                )

    results = []
    # Required fields that a real data row must have (not empty)
    required_fields = {"event", "odds", "sportsbook"}
    for row_idx, row in enumerate(rows):
        d = {}
        for i, col in enumerate(mapped):
            val = row[i] if i < len(row) else ""
            # Strip whitespace and invisible characters from all values
            d[col] = val.strip() if isinstance(val, str) else val
        # Merge separate sport + league into sport_league
        if "sport" in d and "league" in d:
            d["sport_league"] = d["league"] if d["league"] else d["sport"]
        elif "sport" in d and "sport_league" not in d:
            d["sport_league"] = d["sport"]
        elif "league" in d and "sport_league" not in d:
            d["sport_league"] = d["league"]
        # Clean odds values (strip stake info)
        if "odds" in d:
            d["odds"] = _clean_odds(d["odds"])
        if "fair_odds" in d:
            d["fair_odds"] = _clean_odds(d["fair_odds"])
        # Attach bet URL extracted from the row's anchor tags
        if row_links and row_idx < len(row_links) and row_links[row_idx]:
            d["bet_url"] = _resolve_url(row_links[row_idx])
        # Skip junk rows (sub-headers, footers) that lack required data fields
        present = {f for f in required_fields if d.get(f)}
        if not present:
            continue
        results.append(d)
    if len(results) < len(rows):
        log.info("Dropped %d junk/header rows, kept %d data rows", len(rows) - len(results), len(results))
    return results


# ---------------------------------------------------------------------------
# API interception
# ---------------------------------------------------------------------------


def setup_api_intercept(page):
    """Attach listeners to capture XHR/fetch responses."""
    captured = []

    def on_response(response):
        url = response.url
        ct = response.headers.get("content-type", "")
        # Capture JSON responses and any .ashx/.asmx/WebMethod calls
        if "json" in ct or any(
            k in url.lower()
            for k in [".ashx", ".asmx", "webmethod", "api/", "handler", "getdata"]
        ):
            try:
                body = response.text()
                captured.append(
                    {
                        "url": url,
                        "method": response.request.method,
                        "status": response.status,
                        "content_type": ct,
                        "body_length": len(body),
                        "body": body[:50000],
                    }
                )
            except Exception:
                captured.append(
                    {
                        "url": url,
                        "method": response.request.method,
                        "status": response.status,
                        "content_type": ct,
                        "body_length": -1,
                        "body": "<could not read>",
                    }
                )

    page.on("response", on_response)
    return captured


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_table(bets):
    """Pretty-print bets to console using rich."""
    try:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        table = Table(title="+EV Bets", show_lines=True)

        display_cols = [c for c in CANONICAL_FIELDS if any(c in b for b in bets)]
        for col in display_cols:
            table.add_column(col.replace("_", " ").title(), overflow="fold")

        for bet in bets:
            row = [str(bet.get(col, "")) for col in display_cols]
            table.add_row(*row)

        console.print(table)
    except ImportError:
        from tabulate import tabulate

        display_cols = [c for c in CANONICAL_FIELDS if any(c in b for b in bets)]
        table_data = [[bet.get(c, "") for c in display_cols] for bet in bets]
        headers = [c.replace("_", " ").title() for c in display_cols]
        print(tabulate(table_data, headers=headers, tablefmt="grid"))


def write_csv(bets, output_dir):
    """Write bets to a timestamped CSV file."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"ev_bets_{ts}.csv"

    fieldnames = CANONICAL_FIELDS[:]
    # Add any extra columns found in data
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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def scrape_ev(
    sportsbooks=None,
    min_ev=DEFAULT_MIN_EV,
    mainlines_only=DEFAULT_MAINLINES_ONLY,
    devig_method=DEFAULT_DEVIG,
    min_books=DEFAULT_MIN_BOOKS,
    max_odds=DEFAULT_MAX_ODDS,
    min_odds=DEFAULT_MIN_ODDS,
    headless=True,
    intercept_api=False,
    chromium_path=None,
    output_dir=Path("."),
):
    """Core scraper function. Returns (bets, csv_path) where bets is a list of
    dicts and csv_path is the Path to the written CSV. Raises RuntimeError if
    no data is found."""

    if sportsbooks is None:
        sportsbooks = DEFAULT_SPORTSBOOKS

    # Build a simple namespace so apply_filters works unchanged
    class _Args:
        pass
    args = _Args()
    args.sportsbooks = sportsbooks
    args.min_ev = min_ev
    args.mainlines_only = mainlines_only
    args.devig_method = devig_method
    args.min_books = min_books
    args.intercept_api = intercept_api

    with sync_playwright() as pw:
        launch_kwargs = {"headless": headless}
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        log.info("Launching browser (headless=%s) …", headless)
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Optional API interception
        api_captured = []
        if intercept_api:
            api_captured = setup_api_intercept(page)

        # Navigate
        log.info("Navigating to %s …", URL)
        try:
            page.goto(URL, wait_until="networkidle", timeout=45_000)
        except PwTimeout:
            log.warning("networkidle timeout — continuing anyway")

        log.info("Page loaded: %s", page.title() or "(no title)")

        # Apply filters
        apply_filters(page, args)

        # Wait for "Loading" indicator to disappear
        try:
            loading = page.locator(
                "text=Loading Data, text=Loading..., "
                "div[class*='loading'], div[class*='spinner']"
            )
            if loading.count() > 0:
                log.info("Waiting for loading indicator to disappear …")
                loading.first.wait_for(state="hidden", timeout=TABLE_TIMEOUT_MS)
        except PwTimeout:
            log.warning("Loading indicator still visible after timeout")

        # Detect and parse table
        log.info("Detecting data table …")
        strategy, headers, raw_rows, raw_links = detect_table(page)

        if not raw_rows:
            log.error("No data rows found after trying all strategies!")
            log.info("Saving debug screenshot and page HTML …")
            try:
                page.screenshot(path="debug_screenshot.png", timeout=10_000)
                log.info("Screenshot saved to debug_screenshot.png")
            except Exception as e:
                log.warning("Could not save screenshot: %s", e)

            with open("debug_page.html", "w", encoding="utf-8") as f:
                f.write(page.content())
            log.info("Page HTML saved to debug_page.html")

            if intercept_api and api_captured:
                with open("api_debug.json", "w", encoding="utf-8") as f:
                    json.dump(api_captured, f, indent=2)
                log.info(
                    "API debug saved to api_debug.json (%d requests)", len(api_captured)
                )

            browser.close()
            raise RuntimeError("No data rows found — check debug_page.html")

        log.info(
            "Table detected via '%s': %d headers, %d rows",
            strategy,
            len(headers),
            len(raw_rows),
        )

        # Map to structured dicts
        bets = rows_to_dicts(headers, raw_rows, row_links=raw_links)
        if bets:
            log.info("Sample bet: %s", bets[0])

        # Client-side sportsbook filter
        sb_lower = {s.lower() for s in sportsbooks}
        if any("sportsbook" in b for b in bets):
            before = len(bets)
            bets = [
                b
                for b in bets
                if b.get("sportsbook")
                and b["sportsbook"].lower() in sb_lower
            ]
            if len(bets) < before:
                log.info(
                    "Filtered sportsbooks: %d → %d rows", before, len(bets)
                )

        # Client-side EV% filter
        if any("ev_pct" in b for b in bets):
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
        if any("odds" in b for b in bets):
            before = len(bets)
            filtered = []
            for b in bets:
                odds_str = b.get("odds", "").replace("+", "").strip()
                try:
                    odds_val = int(odds_str)
                    if min_odds <= odds_val <= max_odds:
                        filtered.append(b)
                except (ValueError, TypeError):
                    filtered.append(b)  # keep rows we can't parse
            bets = filtered
            if len(bets) < before:
                log.info(
                    "Filtered odds range (%d to +%d): %d → %d rows",
                    min_odds, max_odds, before, len(bets),
                )

        # Write CSV
        csv_path = write_csv(bets, output_dir) if bets else None

        # API debug output
        if intercept_api and api_captured:
            with open("api_debug.json", "w", encoding="utf-8") as f:
                json.dump(api_captured, f, indent=2)
            log.info(
                "API debug saved to api_debug.json (%d requests captured)",
                len(api_captured),
            )
            for req in api_captured:
                if req["status"] == 200 and req["body_length"] > 500:
                    log.info(
                        "  Potential API endpoint: %s %s (%d bytes)",
                        req["method"],
                        req["url"][:150],
                        req["body_length"],
                    )

        browser.close()
        log.info("Done — %d bets found.", len(bets))
        return bets, csv_path


def main():
    args = parse_args()

    try:
        bets, csv_path = scrape_ev(
            sportsbooks=args.sportsbooks,
            min_ev=args.min_ev,
            mainlines_only=args.mainlines_only,
            devig_method=args.devig_method,
            min_books=args.min_books,
            max_odds=args.max_odds,
            min_odds=args.min_odds,
            headless=args.headless,
            intercept_api=args.intercept_api,
            chromium_path=args.chromium_path,
            output_dir=args.output_dir,
        )
    except RuntimeError:
        sys.exit(1)

    if not bets:
        log.warning("No bets remaining after filters!")
        sys.exit(0)

    print_table(bets)
    log.info("Done.")


if __name__ == "__main__":
    main()
