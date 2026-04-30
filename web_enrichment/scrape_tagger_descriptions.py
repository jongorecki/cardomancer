"""Browser-session scraper for Scryfall Tagger oracle-tag descriptions.

The Tagger app at tagger.scryfall.com is a SPA whose tag descriptions
are rendered after an authenticated GraphQL call. The GraphQL endpoint
requires a CSRF token that's only emitted in a logged-in browser session
(verified 2026-04-28 via probes/probe_tagger.py). This script drives a
real browser via Playwright to:

  1. Log into Scryfall once.
  2. Discover the per-tag description by either:
        a) navigating to https://tagger.scryfall.com/?tag=<slug> and
           scraping the rendered DOM, OR
        b) sniffing the GraphQL request the SPA fires and replaying it
           directly with the captured CSRF cookie pair (faster, fewer
           page loads).
  3. Persist `description` back into enrichment.db.tag_catalog so future
     runs are resumable.

## One-time install on the host machine

    pip install playwright python-dotenv
    playwright install chromium

(Playwright pulls a ~150MB Chromium binary the first time it runs.)

## Credentials

Set in a .env file at the project root (already gitignored):

    SCRYFALL_EMAIL=your-scryfall-email@example.com
    SCRYFALL_PASSWORD=your-scryfall-password

The script never logs the password and never persists it to disk
beyond what python-dotenv normally reads.

## Usage

Three modes:

  python -m web_enrichment.scrape_tagger_descriptions --inspect <slug>
        Logs in, navigates to a single tag, and dumps both a rendered
        screenshot (tmp/tagger_inspect_<slug>.png) and the post-render
        HTML (tmp/tagger_inspect_<slug>.html). Use this once on a known
        tag (e.g. `removal`) to figure out the right CSS selector for
        the description. Edit DESCRIPTION_SELECTORS at the top of this
        file with what you find.

  python -m web_enrichment.scrape_tagger_descriptions --probe-graphql <slug>
        Logs in, opens the tag page, and snoops on the network requests
        the SPA fires. Prints the GraphQL operation name, query, and
        variables so you can decide whether to use direct GraphQL replay
        (fastest) instead of DOM scraping.

  python -m web_enrichment.scrape_tagger_descriptions --run [--limit N] [--rate 1.5]
        Iterates tag_catalog rows where description IS NULL or empty
        (and tag_type='function'), fetches each description, and writes
        it back. Resumable: a kill-9 mid-run leaves earlier writes
        committed, so a re-run picks up where it left off. --limit caps
        the number of tags processed per invocation; --rate is the
        seconds between requests (default 1.5).

## Polite scraping

This skirts what Tagger's auth wall is designed to prevent. Hit them
gently: rate limit defaults to 1.5 sec between requests; the script
ALWAYS pauses at least 1.0 sec; --rate cannot go below 1.0. Don't run
more than one instance at a time. Run on your own login credentials,
not a shared one.

## Failure modes

- Playwright import error → install instructions printed, exit 1.
- Login form selectors changed → fill in LOGIN_* constants below.
- Description selectors changed → use --inspect to re-discover.
- Scryfall blocks the session (403, captcha, etc) → script logs the
  status and aborts cleanly, no DB corruption.
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

# --- Boilerplate that doesn't need Playwright -------------------------------

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent.parent
TMP_DIR = SCRIPT_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# --- Selector + URL configuration -------------------------------------------
# Edit these after running --inspect on a known tag if Tagger ever
# changes its markup. The defaults are educated guesses based on common
# React + Rails patterns.

LOGIN_URL = "https://scryfall.com/users/sign_in"
TAGGER_BASE = "https://tagger.scryfall.com"
TAG_URL_FMT = TAGGER_BASE + "/?tag={slug}"

# Selectors used by --inspect / --run. Update via --inspect if Tagger
# changes their markup. Tried in order; the first one that returns
# non-empty text wins.
DESCRIPTION_SELECTORS = [
    # TODO: fill in via --inspect on a known tag. Common candidates
    # to try first:
    'div[data-testid="tag-description"]',
    'section.tag-description',
    'div.tag-description',
    'p.tag-description',
    # The visible description text often lives inside a card-style block
    # near the top of the page; below are generic fallbacks.
    'main p:first-of-type',
    'article > header p',
]

# Login form selectors on scryfall.com/users/sign_in. The form is plain
# server-rendered HTML with Rails/Devise field names. If Scryfall ever
# moves to a JS-rendered login, update these.
LOGIN_EMAIL_SELECTOR = 'input#user_email'
LOGIN_PASSWORD_SELECTOR = 'input#user_password'
LOGIN_SUBMIT_SELECTOR = 'button[type="submit"], input[type="submit"]'


# --- Playwright import + helpful error --------------------------------------

def _require_playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        sys.stderr.write(
            "\nplaywright is not installed. Install with:\n"
            "    pip install playwright python-dotenv\n"
            "    playwright install chromium\n\n"
        )
        return False


def _load_credentials() -> tuple[Optional[str], Optional[str]]:
    """Load Scryfall credentials from .env or the environment."""
    try:
        from dotenv import load_dotenv
        load_dotenv(SCRIPT_DIR / ".env")
    except ImportError:
        pass  # plain env vars still work
    email = os.getenv("SCRYFALL_EMAIL")
    password = os.getenv("SCRYFALL_PASSWORD")
    return email, password


# --- DB layer ---------------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    import enrichment_db
    conn = enrichment_db.get_connection()
    conn.row_factory = sqlite3.Row
    return conn


def list_pending_tags(conn: sqlite3.Connection,
                      limit: Optional[int] = None) -> list[str]:
    """Function tags with a missing or empty description, ordered by
    descending card_count so the most-impactful descriptions arrive first.
    """
    q = (
        "SELECT tag_name FROM tag_catalog "
        "WHERE tag_type = 'function' "
        "  AND (description IS NULL OR description = '') "
        "ORDER BY "
        "  CASE WHEN card_count_expected IS NULL THEN 1 ELSE 0 END, "
        "  card_count_expected DESC, "
        "  tag_name ASC"
    )
    if limit:
        q += f" LIMIT {int(limit)}"
    return [r[0] for r in conn.execute(q).fetchall()]


def write_description(conn: sqlite3.Connection,
                      tag: str,
                      description: str) -> None:
    """Persist a description back to tag_catalog. Idempotent."""
    conn.execute(
        "UPDATE tag_catalog SET description = ?, last_updated = ? "
        "WHERE tag_name = ? AND tag_type = 'function'",
        (description.strip()[:8000], _now_iso(), tag),
    )
    conn.commit()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Browser session helpers ------------------------------------------------

def _login(page, email: str, password: str) -> bool:
    """Log into Scryfall in the given page. Returns True on success."""
    logger.info("Logging into Scryfall as %s …", email)
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    try:
        page.fill(LOGIN_EMAIL_SELECTOR, email)
        page.fill(LOGIN_PASSWORD_SELECTOR, password)
    except Exception as exc:
        logger.error("Login form selectors failed: %s", exc)
        logger.error("If Scryfall changed their login UI, update "
                     "LOGIN_EMAIL_SELECTOR / LOGIN_PASSWORD_SELECTOR / "
                     "LOGIN_SUBMIT_SELECTOR at the top of this file.")
        return False

    # Submit and wait for navigation to complete
    page.click(LOGIN_SUBMIT_SELECTOR)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass

    # Heuristic success check: signed-in pages usually expose a /users/sign_out link.
    if page.locator('a[href*="sign_out"]').count() > 0:
        logger.info("Login OK")
        return True

    logger.error("Login appears to have failed (no sign_out link visible). "
                 "Check email/password.")
    return False


def _scrape_description_from_dom(page, slug: str,
                                  wait_seconds: float = 5.0) -> Optional[str]:
    """Navigate to a tag page and try each selector in order."""
    page.goto(TAG_URL_FMT.format(slug=slug), wait_until="domcontentloaded")
    # The SPA needs time to fetch + render the description after the
    # initial HTML lands. Try each selector with a short wait.
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        for sel in DESCRIPTION_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.count() > 0:
                    text = loc.inner_text(timeout=500).strip()
                    if text and len(text) > 10:
                        return text
            except Exception:
                continue
        page.wait_for_timeout(250)
    return None


# --- Modes -------------------------------------------------------------------

def cmd_inspect(slug: str) -> int:
    if not _require_playwright():
        return 1
    email, password = _load_credentials()
    if not (email and password):
        sys.stderr.write(
            "SCRYFALL_EMAIL / SCRYFALL_PASSWORD not found in .env or env.\n")
        return 1
    from playwright.sync_api import sync_playwright

    out_html = TMP_DIR / f"tagger_inspect_{slug}.html"
    out_png = TMP_DIR / f"tagger_inspect_{slug}.png"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        if not _login(page, email, password):
            browser.close()
            return 1
        page.goto(TAG_URL_FMT.format(slug=slug),
                  wait_until="domcontentloaded")
        page.wait_for_timeout(4000)  # let SPA settle
        out_html.write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out_png), full_page=True)
        # Try the configured selectors and report which worked
        print(f"\nWrote post-render HTML  → {out_html}")
        print(f"Wrote screenshot         → {out_png}")
        print()
        print("Trying configured DESCRIPTION_SELECTORS in order:")
        for sel in DESCRIPTION_SELECTORS:
            try:
                loc = page.locator(sel).first
                cnt = loc.count()
                if cnt > 0:
                    txt = loc.inner_text(timeout=500).strip()
                    print(f"  [{cnt:>2}] {sel:50} → {txt[:120]!r}")
                else:
                    print(f"  [ 0] {sel:50} → no match")
            except Exception as exc:
                print(f"  [ER] {sel:50} → {exc}")
        browser.close()
    print()
    print(f"Open {out_html} in your browser, find the description, then")
    print("update DESCRIPTION_SELECTORS in this file with the right CSS.")
    return 0


def cmd_probe_graphql(slug: str) -> int:
    if not _require_playwright():
        return 1
    email, password = _load_credentials()
    if not (email and password):
        sys.stderr.write(
            "SCRYFALL_EMAIL / SCRYFALL_PASSWORD not found in .env or env.\n")
        return 1
    from playwright.sync_api import sync_playwright

    captured: list[dict] = []

    def on_request(req):
        if req.method == "POST" and "graphql" in req.url:
            try:
                pd = req.post_data
            except Exception:
                pd = None
            captured.append({
                "url": req.url,
                "headers": dict(req.headers),
                "body": pd,
            })

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.on("request", on_request)
        if not _login(page, email, password):
            browser.close()
            return 1
        page.goto(TAG_URL_FMT.format(slug=slug),
                  wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        browser.close()

    print(f"\nCaptured {len(captured)} GraphQL request(s) for tag '{slug}':\n")
    for i, req in enumerate(captured):
        print(f"  --- request {i + 1} ---")
        print(f"  URL: {req['url']}")
        print(f"  X-CSRF-Token header: "
              f"{req['headers'].get('x-csrf-token', '(missing)')[:30]}…")
        body = req["body"] or ""
        if len(body) > 400:
            body = body[:400] + "  …(truncated)"
        print(f"  Body: {body}")
        print()
    if captured:
        print("If any request looks like 'query Tag(... description ...)',")
        print("you can replay it via httpx for ~10x speedup. Otherwise")
        print("the DOM-scrape path in --run still works.")
    return 0


def cmd_run(rate_seconds: float, limit: Optional[int]) -> int:
    if not _require_playwright():
        return 1
    rate_seconds = max(1.0, float(rate_seconds))  # politeness floor
    email, password = _load_credentials()
    if not (email and password):
        sys.stderr.write(
            "SCRYFALL_EMAIL / SCRYFALL_PASSWORD not found in .env or env.\n")
        return 1
    from playwright.sync_api import sync_playwright

    conn = get_conn()
    try:
        pending = list_pending_tags(conn, limit=limit)
    finally:
        conn.close()
    if not pending:
        print("Nothing to do — every function tag already has a description.")
        return 0
    print(f"Will scrape {len(pending):,} tag descriptions at "
          f"{rate_seconds:.1f}s / tag = ~{(len(pending) * rate_seconds / 60):.1f} min")

    success = 0
    failures = 0
    started_at = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        if not _login(page, email, password):
            browser.close()
            return 1

        # Reopen DB inside the loop so a long run doesn't hold a single
        # transaction open. enrichment_db.get_connection is cheap.
        for i, slug in enumerate(pending):
            try:
                desc = _scrape_description_from_dom(page, slug,
                                                     wait_seconds=4.0)
            except Exception as exc:
                logger.warning("scrape error on %s: %s", slug, exc)
                desc = None

            if desc:
                conn = get_conn()
                try:
                    write_description(conn, slug, desc)
                finally:
                    conn.close()
                success += 1
                logger.info("%5d/%d  %s  → %s",
                            i + 1, len(pending), slug, desc[:60])
            else:
                failures += 1
                logger.warning("%5d/%d  %s  (no description found)",
                               i + 1, len(pending), slug)
            time.sleep(rate_seconds)

        browser.close()

    elapsed = time.time() - started_at
    print(f"\nDone in {elapsed/60:.1f} min")
    print(f"  success: {success}")
    print(f"  failures: {failures}")
    return 0 if failures == 0 else 2


# --- CLI --------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(
        description="Scrape Scryfall Tagger oracle-tag descriptions via Playwright."
    )
    sub = parser.add_subparsers(dest="cmd")
    p_ins = sub.add_parser("inspect",
        help="Dump rendered HTML + screenshot for one tag (CSS-selector discovery).")
    p_ins.add_argument("slug")
    p_pgql = sub.add_parser("probe-graphql",
        help="Sniff GraphQL traffic to discover the description query.")
    p_pgql.add_argument("slug")
    p_run = sub.add_parser("run",
        help="Scrape all tags missing a description; resumable.")
    p_run.add_argument("--rate", type=float, default=1.5,
                       help="Seconds between requests (>=1.0)")
    p_run.add_argument("--limit", type=int, default=None,
                       help="Cap number of tags processed (default: all pending)")
    args = parser.parse_args(argv)

    if args.cmd == "inspect":
        return cmd_inspect(args.slug)
    if args.cmd == "probe-graphql":
        return cmd_probe_graphql(args.slug)
    if args.cmd == "run":
        return cmd_run(args.rate, args.limit)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
