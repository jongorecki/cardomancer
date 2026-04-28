# web_enrichment/tagger.py
# ---------------------------------------------------------------------------
# TaggerSource: populates the `tags`, `art_tags`, and `tag_catalog` tables
# using the Scryfall search API's `otag:` / `atag:` syntax.
#
# Why not Tagger GraphQL directly?
# The live probe (2026-04-20) confirmed that the GraphQL endpoint at
# tagger.scryfall.com/graphql requires a CSRF authenticity token obtained
# from a browser session — unauthenticated POST returns:
#   {'success': False, 'message': 'invalid authenticity token'}
# Building a headless browser session is fragile and out of scope for
# Phase 1. The search-API fallback is fully documented, rate-limited
# conservatively, and produces the same tag → card mapping.
#
# Approach (fallback path per plans/web_enrichment_source_probes.md §3):
#   1. Enumerate all known `otag:` names from KNOWN_FUNCTION_TAGS.
#   2. For each tag, paginate the Scryfall search API `?q=otag:<name>`.
#   3. Store (oracle_id, tag_name, source='scryfall_search') in `tags`.
#   4. Write `tag_catalog` rows with card_count_expected from first-page
#      `total_cards` field.
#   5. Art tags (`atag:`) follow the same pattern using KNOWN_ART_TAGS,
#      keyed on printing_id (Scryfall `id` field).
#
# Incremental refresh: re-pull only tags whose expected count changed vs
# last run (read from tag_catalog). Full refresh always re-pulls all.
#
# Rate limit: 100ms between requests (Scryfall's documented safe interval).
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

import enrichment_db
from web_enrichment.base import EmitFn, EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)

SEARCH_URL = "https://api.scryfall.com/cards/search"
RATE_LIMIT_S = 1.0  # 1 sec between requests
# Note: Scryfall's docs suggest 50-100ms is "safe" but their per-IP rate
# limiter throttles HARD on sustained loads of 5000+ requests (a full
# TaggerSource refresh). Two empirical data points (2026-04-28):
#   - 100ms: 838 HTTP 429s in 42 min (40% of requests). Total: ~7+ hr.
#   - 400ms: same pattern after ~10 min — the limiter slots us into a
#            stricter band once total volume crosses some threshold.
# 1 sec/req gives us enough headroom that Scryfall's sustained-load
# detection doesn't trip. Total otag-only run: ~5184 tags × 1.5 pages
# avg × 1 sec = ~2 hours wallclock. Slow but reliable.
TIMEOUT_S = 30
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"

# ---------------------------------------------------------------------------
# Comprehensive list of known Scryfall function tags (otag:).
# Source: community knowledge + Scryfall tagger UI categories.
# Organized by logical category for maintainability.
# ---------------------------------------------------------------------------
KNOWN_FUNCTION_TAGS: list[str] = [
    # Removal — spot
    "removal", "spot-removal", "exile-removal", "bounce-removal",
    "tuck-removal", "sacrifice-removal", "destroy-target-creature",
    # Removal — mass
    "wrath", "sweeper", "symmetrical-wrath", "asymmetrical-wrath",
    "board-wipe",
    # Counter magic
    "counterspell", "soft-counterspell", "hard-counterspell",
    "permanent-counterspell",
    # Card draw / card advantage
    "draw", "cantrip", "card-draw", "looting", "rummaging",
    "impulse", "card-advantage", "extra-draw",
    # Tutors / search
    "tutor", "creature-tutor", "artifact-tutor", "land-tutor",
    "enchantment-tutor", "instant-tutor", "sorcery-tutor",
    "any-card-tutor",
    # Ramp
    "ramp", "land-ramp", "mana-rock", "mana-dork", "ritual",
    "cost-reduction",
    # Protection
    "protection", "hexproof", "shroud", "indestructible", "ward",
    "phasing", "flicker-protection", "granting-protection",
    "totem-armor",
    # Evasion
    "evasion", "flying", "trample", "unblockable", "shadow",
    "intimidate", "fear", "menace", "deathtouch", "lifelink",
    "reach", "first-strike", "double-strike", "haste",
    # Tokens
    "token", "token-generation", "token-payoff", "populate",
    "go-wide",
    # +1/+1 counters
    "counters", "counter-manipulation", "proliferate",
    "counter-payoff",
    # Graveyard
    "graveyard-recursion", "self-mill", "reanimation", "flashback",
    "escape", "delve", "threshold", "dredge", "undergrowth",
    # Combo enablers
    "combo", "infinite-combo", "two-card-combo", "win-condition",
    "game-ending",
    # Stax / disruption
    "stax", "tax", "symmetrical-tax", "asymmetrical-tax",
    "land-destruction", "hand-disruption", "discard",
    # Life gain / drain
    "lifegain", "lifedrain", "lifegain-payoff",
    # Voltron / Auras
    "aura", "equipment", "voltron", "power-boosting",
    # Lands / land-related
    "fetch-land", "dual-land", "shock-land", "check-land",
    "bounce-land", "pain-land", "triome", "cycling-land",
    # Utility
    "mana-fixing", "color-fixing",
    "sacrifice-outlet", "free-sacrifice-outlet",
    "instant-speed", "flash", "free-spell",
    "cantrip", "looting", "cycling",
    # Card types / layouts
    "vanilla", "french-vanilla", "keyword-only",
    "saga", "class", "battle", "adventure", "modal-dfc",
    # Archetypes / themes
    "aristocrats", "spellslinger", "stompy", "blink",
    "good-stuff", "big-mana",
]

# Deduplicate preserving order
_seen_fn: set[str] = set()
_FN: list[str] = []
for _t in KNOWN_FUNCTION_TAGS:
    if _t not in _seen_fn:
        _FN.append(_t)
        _seen_fn.add(_t)
KNOWN_FUNCTION_TAGS = _FN

# Art tags (atag:) — smaller set, keyed on printing_id
KNOWN_ART_TAGS: list[str] = [
    "full-art", "borderless", "extended-art", "showcase",
    "retro-frame", "phyrexian-frame",
    "foil-etched", "textured-foil",
    "art-series",
    "alternate-art",
]


class TaggerSource(EnrichmentSource):
    """Populate tags / art_tags / tag_catalog via Scryfall search-API fallback.

    The Tagger GraphQL endpoint requires session auth (CSRF token) that is
    impractical to automate. This implementation uses the documented Scryfall
    search API `otag:` / `atag:` syntax to build the same tag→card mapping.
    A future Phase (or a manual auth helper) can replace this with the native
    Tagger GraphQL once the auth story is solved.
    """

    name = "tagger"

    def probe(self) -> bool:
        from probes.probe_tagger import probe as _probe
        r = _probe()
        # Tagger probe returns ok=True even in fallback mode (auth warning).
        # We additionally verify the Scryfall search API is reachable.
        return r.ok and self._scryfall_reachable()

    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        start = time.time()
        errors: list[str] = []
        warnings: list[str] = [
            "Using Scryfall search-API fallback (Tagger GraphQL requires "
            "session auth; see probe_tagger.py warnings)."
        ]

        if not self._scryfall_reachable():
            msg = "Scryfall search API unreachable; aborting tagger refresh."
            errors.append(msg)
            _record(self.name, False, msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)

        # Load previous expected counts to detect which tags changed
        prev_expected = _load_prev_expected() if not full else {}

        # Load tag lists from tag_catalog if available (populated by
        # TaggerCatalogueSource.refresh()); fall back to hardcoded lists
        # for cold-start / offline usage.
        function_tags = _load_tags_from_catalog("function") or KNOWN_FUNCTION_TAGS
        art_tags_list = _load_tags_from_catalog("art") or KNOWN_ART_TAGS

        tag_rows: list[dict] = []
        art_tag_rows: list[dict] = []
        catalog_rows: list[dict] = []
        coverage_errors: list[str] = []

        total_tags = len(function_tags) + len(art_tags_list)
        done = 0
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with httpx.Client(
            timeout=TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            # Function tags
            for tag in function_tags:
                done += 1
                if done % 20 == 1:
                    _emit_progress(emit, done, total_tags,
                                   f"Fetching otag:{tag} …")

                total_expected = prev_expected.get(tag)
                cards, total = self._fetch_tag(
                    client, f"otag:{tag}", warnings)

                if total is None:
                    # tag returned no results (expected for new/missing tags)
                    catalog_rows.append({
                        "tag_name": tag,
                        "tag_type": "function",
                        "parent": None,
                        "description": None,
                        "card_count_expected": 0,
                        "source": "fallback",
                        "last_updated": ts,
                    })
                    continue

                # Check incremental: if count unchanged, skip re-pull
                if not full and total_expected == total and total_expected > 0:
                    catalog_rows.append({
                        "tag_name": tag,
                        "tag_type": "function",
                        "parent": None,
                        "description": None,
                        "card_count_expected": total,
                        "source": "fallback",
                        "last_updated": ts,
                    })
                    continue

                for oracle_id in cards:
                    tag_rows.append({
                        "oracle_id": oracle_id,
                        "tag_name": tag,
                        "source": "scryfall_search",
                    })

                actual = len(cards)
                if total > 0 and abs(actual - total) / total > 0.01:
                    coverage_errors.append(
                        f"otag:{tag}: expected {total} cards, got {actual}")

                catalog_rows.append({
                    "tag_name": tag,
                    "tag_type": "function",
                    "parent": None,
                    "description": None,
                    "card_count_expected": total,
                    "source": "fallback",
                    "last_updated": ts,
                })

            # Art tags
            for tag in art_tags_list:
                done += 1
                if done % 10 == 1:
                    _emit_progress(emit, done, total_tags,
                                   f"Fetching atag:{tag} …")
                printing_ids, total = self._fetch_atag(
                    client, f"atag:{tag}", warnings)
                if total is None:
                    continue
                for pid in printing_ids:
                    art_tag_rows.append({
                        "printing_id": pid,
                        "tag_name": tag,
                        "source": "scryfall_search",
                    })
                catalog_rows.append({
                    "tag_name": f"atag:{tag}",
                    "tag_type": "art",
                    "parent": None,
                    "description": None,
                    "card_count_expected": total,
                    "source": "fallback",
                    "last_updated": ts,
                })

        if coverage_errors:
            warnings.extend(coverage_errors)

        _emit_progress(emit, total_tags, total_tags,
                       f"Writing {len(tag_rows)} tag rows to DB …")

        conn = enrichment_db.get_connection()
        rows_changed = 0
        try:
            rows_changed = self._write(
                conn, tag_rows, art_tag_rows, catalog_rows, warnings)
            total_tags_in_db = conn.execute(
                "SELECT COUNT(*) FROM tags").fetchone()[0]
            coverage_pct = (
                float(total_tags_in_db) /
                max(1, len(tag_rows)) * 100
                if tag_rows else 100.0
            )
            enrichment_db.record_sync_attempt(
                conn, self.name, success=True,
                coverage_pct=coverage_pct,
                version_hash=str(len(catalog_rows)),
            )
            enrichment_db.record_coverage(
                conn, self.name, key_name="function_tags",
                expected=len(tag_rows),
                actual=total_tags_in_db,
            )
        except Exception as exc:
            msg = f"DB write error: {type(exc).__name__}: {exc}"
            logger.exception("Tagger DB write error")
            errors.append(msg)
            enrichment_db.record_sync_attempt(conn, self.name, False, error=msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)
        finally:
            conn.close()

        return RefreshResult(
            source=self.name,
            success=True,
            duration_ms=_ms(start),
            rows_changed=rows_changed,
            coverage_pct=coverage_pct,
            warnings=warnings,
        )

    def coverage_report(self) -> dict:
        conn = enrichment_db.get_connection()
        try:
            sync = conn.execute(
                "SELECT * FROM sync_metadata WHERE source = ?",
                (self.name,)
            ).fetchone()
            tag_count = conn.execute(
                "SELECT COUNT(DISTINCT tag_name) FROM tags"
            ).fetchone()[0]
            oracle_count = conn.execute(
                "SELECT COUNT(DISTINCT oracle_id) FROM tags"
            ).fetchone()[0]
            art_count = conn.execute(
                "SELECT COUNT(DISTINCT tag_name) FROM art_tags"
            ).fetchone()[0]
            return {
                "source": self.name,
                "distinct_tags": tag_count,
                "tagged_oracle_ids": oracle_count,
                "distinct_art_tags": art_count,
                "fallback_mode": True,
                **(dict(sync) if sync else {}),
            }
        finally:
            conn.close()

    # -- Fetch helpers -------------------------------------------------------

    @staticmethod
    def _fetch_tag(
        client: httpx.Client,
        query: str,
        warnings: list[str],
    ) -> tuple[list[str], Optional[int]]:
        """Paginate Scryfall search API for a function tag.

        Returns (oracle_ids, total_count). Returns ([], None) on 404 (tag
        not found) or on repeated errors.
        """
        oracle_ids: list[str] = []
        url: Optional[str] = SEARCH_URL
        params: dict = {"q": query, "page": 1}
        total: Optional[int] = None
        retries = 0

        while url:
            time.sleep(RATE_LIMIT_S)
            try:
                if params:
                    resp = client.get(url, params=params)
                    params = {}
                else:
                    resp = client.get(url)
            except Exception as exc:
                logger.debug("Scryfall search %s error: %s", query, exc)
                break

            if resp.status_code == 404:
                return [], None
            if resp.status_code == 429:
                retries += 1
                if retries > 3:
                    warnings.append(f"Rate-limit retries exceeded for {query}")
                    break
                time.sleep(2.0)
                continue
            if not resp.is_success:
                break

            data = resp.json()
            if total is None:
                total = data.get("total_cards")

            for card in data.get("data") or []:
                oid = card.get("oracle_id")
                if oid:
                    oracle_ids.append(oid)

            url = data.get("next_page")
            retries = 0

        return oracle_ids, total

    @staticmethod
    def _fetch_atag(
        client: httpx.Client,
        query: str,
        warnings: list[str],
    ) -> tuple[list[str], Optional[int]]:
        """Like _fetch_tag but collects printing `id` instead of oracle_id."""
        printing_ids: list[str] = []
        url: Optional[str] = SEARCH_URL
        params: dict = {"q": query, "page": 1}
        total: Optional[int] = None
        retries = 0

        while url:
            time.sleep(RATE_LIMIT_S)
            try:
                if params:
                    resp = client.get(url, params=params)
                    params = {}
                else:
                    resp = client.get(url)
            except Exception as exc:
                logger.debug("Scryfall search %s error: %s", query, exc)
                break

            if resp.status_code == 404:
                return [], None
            if resp.status_code == 429:
                retries += 1
                if retries > 3:
                    warnings.append(f"Rate-limit exceeded for {query}")
                    break
                time.sleep(2.0)
                continue
            if not resp.is_success:
                break

            data = resp.json()
            if total is None:
                total = data.get("total_cards")

            for card in data.get("data") or []:
                pid = card.get("id")  # printing_id for art tags
                if pid:
                    printing_ids.append(pid)

            url = data.get("next_page")
            retries = 0

        return printing_ids, total

    @staticmethod
    def _scryfall_reachable() -> bool:
        try:
            r = httpx.get(
                SEARCH_URL,
                params={"q": "otag:removal", "page": 1},
                timeout=10,
                headers={"User-Agent": USER_AGENT},
            )
            return r.is_success or r.status_code == 404
        except Exception:
            return False

    @staticmethod
    def _write(
        conn,
        tag_rows: list[dict],
        art_tag_rows: list[dict],
        catalog_rows: list[dict],
        warnings: list[str],
    ) -> int:
        changed = 0
        with conn:
            conn.executemany(
                """INSERT INTO tags (oracle_id, tag_name, source)
                   VALUES (:oracle_id, :tag_name, :source)
                   ON CONFLICT(oracle_id, tag_name) DO UPDATE SET
                       source = excluded.source""",
                tag_rows,
            )
            changed += len(tag_rows)

            conn.executemany(
                """INSERT INTO art_tags (printing_id, tag_name, source)
                   VALUES (:printing_id, :tag_name, :source)
                   ON CONFLICT(printing_id, tag_name) DO UPDATE SET
                       source = excluded.source""",
                art_tag_rows,
            )
            changed += len(art_tag_rows)

            conn.executemany(
                """INSERT INTO tag_catalog
                       (tag_name, tag_type, parent, description,
                        card_count_expected, source)
                   VALUES (:tag_name, :tag_type, :parent, :description,
                           :card_count_expected, :source)
                   ON CONFLICT(tag_name) DO UPDATE SET
                       card_count_expected = excluded.card_count_expected,
                       source = excluded.source""",
                catalog_rows,
            )
            changed += len(catalog_rows)
        return changed


def _load_prev_expected() -> dict[str, int]:
    """Load tag_name → card_count_expected from the last run."""
    conn = enrichment_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT tag_name, card_count_expected FROM tag_catalog "
            "WHERE tag_type = 'function'"
        ).fetchall()
        return {r["tag_name"]: r["card_count_expected"] or 0 for r in rows}
    finally:
        conn.close()


def _load_tags_from_catalog(tag_type: str) -> list[str]:
    """Return tag names from tag_catalog for a given tag_type.

    Returns an empty list (falsy) if tag_catalog has no rows for that type,
    which triggers the hardcoded-list fallback in TaggerSource.refresh().
    Art tags stored in tag_catalog may include an 'atag:' prefix from the
    old hardcoded list — strip it for consistency.
    """
    conn = enrichment_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT tag_name FROM tag_catalog WHERE tag_type = ?",
            (tag_type,)
        ).fetchall()
        names: list[str] = []
        for r in rows:
            name = r["tag_name"]
            # Old hardcoded art tags were stored as 'atag:<slug>'; strip prefix.
            if name.startswith("atag:"):
                name = name[5:]
            names.append(name)
        return names
    except Exception:
        return []
    finally:
        conn.close()


def _emit_progress(emit: Optional[EmitFn], step: int, total: int,
                   message: str) -> None:
    if emit is None:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        emit("enrichment_refresh_progress", {
            "source": "tagger",
            "step": step, "total": total,
            "message": message, "ts": ts,
        })
    except Exception:
        pass


def _record(source: str, success: bool, error: Optional[str] = None) -> None:
    conn = enrichment_db.get_connection()
    try:
        enrichment_db.record_sync_attempt(conn, source, success, error=error)
    finally:
        conn.close()


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)
