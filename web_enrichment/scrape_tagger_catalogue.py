# web_enrichment/scrape_tagger_catalogue.py
# ---------------------------------------------------------------------------
# TaggerCatalogueSource: fetches the full Scryfall Tagger catalogue from
# https://scryfall.com/docs/tagger-tags and upserts every otag and atag
# into the tag_catalog table.
#
# Why this exists:
#   TaggerSource uses a hardcoded list of ~150 KNOWN_FUNCTION_TAGS and ~11
#   KNOWN_ART_TAGS. The docs page lists ~5000 otags and ~11000 atags.
#   Running this source first expands tag_catalog so TaggerSource.refresh()
#   can pick up the full list on its next run.
#
# Parsing strategy:
#   The docs page has 27 pairs of <h2> sections — each letter has both a
#   plain section (art tags, links use art%3A / art: query syntax) and a
#   "(functional)" section (otags, links use oracletag%3A syntax).
#   We extract all hrefs matching:
#     - /search?q=oracletag%3A<slug>          → tag_type='function'
#     - /search?q=art%3A<slug>&unique=art      → tag_type='art'
#   The &unique=art suffix distinguishes catalogue art-tag links from the
#   one-off art: example link in the page intro.
#
# Cadence: monthly (the docs page rarely changes by more than a handful of
# tags between Scryfall releases).
#
# Rate limit: one HTTP request — no rate limiting needed beyond politeness.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote

import httpx

import enrichment_db
from web_enrichment.base import EmitFn, EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)

DOCS_URL = "https://scryfall.com/docs/tagger-tags"
TIMEOUT_S = 15
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"

# Regex patterns for extracting tag slugs from the docs page HTML.
# oracletag%3A<slug> — functional tags (otags)
_OTAG_RE = re.compile(r'href="/search\?q=oracletag%3A([^"]+)"')
# art%3A<slug>&amp;unique=art — art tags (atags); the &amp;unique=art suffix
# distinguishes catalogue entries from the prose example link in the intro.
_ATAG_RE = re.compile(r'href="/search\?q=art%3A([^"]+)&amp;unique=art"')


def parse_catalogue_html(html: str) -> tuple[list[str], list[str]]:
    """Parse the tagger-tags docs page HTML.

    Returns (otag_slugs, atag_slugs). Slugs are URL-decoded strings
    (e.g. 'wrath-effect', 'full-art').
    """
    otag_slugs = [unquote(m) for m in _OTAG_RE.findall(html)]
    atag_slugs = [unquote(m) for m in _ATAG_RE.findall(html)]
    return otag_slugs, atag_slugs


class TaggerCatalogueSource(EnrichmentSource):
    """Fetch the full Scryfall Tagger tag catalogue from the docs page.

    Expands tag_catalog from the ~150-tag hardcoded fallback list to the full
    ~5000+ tag universe. TaggerSource.refresh() reads tag_catalog after this
    runs to know which tags to pull card data for.

    Refresh cadence: monthly. One HTTP request per run.
    """

    name = "tagger_catalogue"

    def probe(self) -> bool:
        """Check that the docs page is reachable and contains tag links."""
        try:
            r = httpx.get(
                DOCS_URL,
                timeout=TIMEOUT_S,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            if not r.is_success:
                logger.warning("tagger_catalogue probe: HTTP %s", r.status_code)
                return False
            # Verify the page contains at least one oracletag link
            return bool(_OTAG_RE.search(r.text))
        except Exception as exc:
            logger.warning("tagger_catalogue probe failed: %s", exc)
            return False

    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        start = time.time()
        errors: list[str] = []
        warnings: list[str] = []

        _emit_progress(emit, 0, 3, "Checking tagger-tags docs page …")

        if not self.probe():
            msg = "Tagger catalogue probe failed; aborting."
            errors.append(msg)
            _record_sync(self.name, success=False, error=msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)

        _emit_progress(emit, 1, 3, f"Fetching {DOCS_URL} …")

        try:
            resp = httpx.get(
                DOCS_URL,
                timeout=TIMEOUT_S,
                headers={"User-Agent": USER_AGENT},
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as exc:
            msg = f"Failed to fetch tagger-tags docs page: {type(exc).__name__}: {exc}"
            errors.append(msg)
            logger.error("tagger_catalogue fetch error: %s", exc)
            _record_sync(self.name, success=False, error=msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)

        otag_slugs, atag_slugs = parse_catalogue_html(resp.text)

        if not otag_slugs:
            msg = ("No otag links found on tagger-tags docs page — "
                   "page structure may have changed.")
            errors.append(msg)
            warnings.append(msg)
            _record_sync(self.name, success=False, error=msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start),
                                 errors=errors, warnings=warnings)

        _emit_progress(emit, 2, 3,
                       f"Upserting {len(otag_slugs)} otags + "
                       f"{len(atag_slugs)} atags into tag_catalog …")

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        catalog_rows: list[dict] = []
        for slug in otag_slugs:
            catalog_rows.append({
                "tag_name": slug,
                "tag_type": "function",
                "parent": None,
                "description": None,
                "card_count_expected": None,
                "source": "docs_page",
                "last_updated": ts,
            })
        for slug in atag_slugs:
            catalog_rows.append({
                "tag_name": slug,
                "tag_type": "art",
                "parent": None,
                "description": None,
                "card_count_expected": None,
                "source": "docs_page",
                "last_updated": ts,
            })

        rows_changed = 0
        conn = enrichment_db.get_connection()
        try:
            rows_changed = _write_catalog(conn, catalog_rows)
            coverage_pct = (
                float(rows_changed) / max(1, len(catalog_rows)) * 100
            )
            enrichment_db.record_sync_attempt(
                conn, self.name, success=True,
                coverage_pct=coverage_pct,
                version_hash=str(len(otag_slugs)),
            )
            enrichment_db.record_coverage(
                conn, self.name, key_name="otags",
                expected=len(otag_slugs),
                actual=len(otag_slugs),
            )
            enrichment_db.record_coverage(
                conn, self.name, key_name="atags",
                expected=len(atag_slugs),
                actual=len(atag_slugs),
            )
        except Exception as exc:
            msg = f"DB write error: {type(exc).__name__}: {exc}"
            logger.exception("tagger_catalogue DB write error")
            errors.append(msg)
            enrichment_db.record_sync_attempt(conn, self.name, False, error=msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)
        finally:
            conn.close()

        _emit_progress(emit, 3, 3,
                       f"Done. {len(otag_slugs)} otags + {len(atag_slugs)} atags "
                       f"written to tag_catalog.")

        return RefreshResult(
            source=self.name,
            success=True,
            duration_ms=_ms(start),
            rows_changed=rows_changed,
            coverage_pct=float(len(catalog_rows)),
            warnings=warnings,
        )

    def coverage_report(self) -> dict:
        conn = enrichment_db.get_connection()
        try:
            sync = conn.execute(
                "SELECT * FROM sync_metadata WHERE source = ?",
                (self.name,)
            ).fetchone()
            otag_count = conn.execute(
                "SELECT COUNT(*) FROM tag_catalog WHERE tag_type='function'"
            ).fetchone()[0]
            atag_count = conn.execute(
                "SELECT COUNT(*) FROM tag_catalog WHERE tag_type='art'"
            ).fetchone()[0]
            return {
                "source": self.name,
                "otag_count": otag_count,
                "atag_count": atag_count,
                **(dict(sync) if sync else {}),
            }
        finally:
            conn.close()


def _write_catalog(conn, catalog_rows: list[dict]) -> int:
    """Upsert tag_catalog rows. Preserves existing card_count_expected + parent.

    Does NOT overwrite card_count_expected or parent with NULL — those are
    populated by TaggerSource.refresh() and the derivation pipeline, which
    run after this scraper.  Only source and description are refreshed.
    """
    changed = 0
    with conn:
        # tag_catalog has a last_updated column only if migration added it.
        # Use a safe upsert that doesn't reference last_updated if the column
        # doesn't exist. Check first.
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tag_catalog)").fetchall()
        }
        has_last_updated = "last_updated" in cols

        if has_last_updated:
            conn.executemany(
                """INSERT INTO tag_catalog
                       (tag_name, tag_type, parent, description,
                        card_count_expected, source, last_updated)
                   VALUES (:tag_name, :tag_type, :parent, :description,
                           :card_count_expected, :source, :last_updated)
                   ON CONFLICT(tag_name) DO UPDATE SET
                       tag_type    = excluded.tag_type,
                       source      = excluded.source,
                       last_updated = excluded.last_updated""",
                catalog_rows,
            )
        else:
            rows_no_ts = [
                {k: v for k, v in r.items() if k != "last_updated"}
                for r in catalog_rows
            ]
            conn.executemany(
                """INSERT INTO tag_catalog
                       (tag_name, tag_type, parent, description,
                        card_count_expected, source)
                   VALUES (:tag_name, :tag_type, :parent, :description,
                           :card_count_expected, :source)
                   ON CONFLICT(tag_name) DO UPDATE SET
                       tag_type = excluded.tag_type,
                       source   = excluded.source""",
                rows_no_ts,
            )
        changed = len(catalog_rows)
    return changed


def _emit_progress(emit: Optional[EmitFn], step: int, total: int,
                   message: str) -> None:
    if emit is None:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        emit("enrichment_refresh_progress", {
            "source": "tagger_catalogue",
            "step": step, "total": total,
            "message": message, "ts": ts,
        })
    except Exception:
        pass


def _record_sync(source: str, success: bool,
                 error: Optional[str] = None) -> None:
    conn = enrichment_db.get_connection()
    try:
        enrichment_db.record_sync_attempt(conn, source, success, error=error)
    finally:
        conn.close()


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)
