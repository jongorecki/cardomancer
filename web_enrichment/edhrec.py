# web_enrichment/edhrec.py
# ---------------------------------------------------------------------------
# EDHRECSource: pulls staple / salt / theme / commander-popularity data from
# json.edhrec.com.
#
# Approach (Phase 1.7 + 1.10):
#   - Universal staple:  inclusion_pct > UNIVERSAL_THRESHOLD on the global
#                        top/year page or a card detail page.
#   - Archetype staple:  card appears in the top-N cards of >= ARCHETYPE_MIN_PAGES
#                        of the union of:
#                          * 32 color top-cards pages (mono/guild/shard/wedge/
#                            4-color/5-color/colorless)
#                          * up to MAX_THEME_PAGES theme top-cards pages (from
#                            the /themes index)
#                          * the top-commanders list page
#                        No curated archetype catalogue is maintained — we rely
#                        entirely on EDHREC's own canonical page listing.
#   - Salt:              directly from card detail page `salt` field
#   - Themes:            each theme page slug is stored as a theme membership row
#                        for every card appearing in that page's top-cards list
#   - Commander pop.:    deck_count from the top-commanders page is written to
#                        the commander_ranks table for each commander cardview
#
# Rate limit: 1 req/sec. Be gentle.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

import enrichment_db
from web_enrichment.base import EmitFn, EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)

RATE_LIMIT_S = 1.0
TIMEOUT_S = 30
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"
BASE = "https://json.edhrec.com/pages"

UNIVERSAL_THRESHOLD = 0.05       # 5% global inclusion → universal staple
ARCHETYPE_MIN_PAGES = 3          # must appear in >= this many pages to get tier
MAX_CARDS_PER_COLOR_PAGE = 100   # top N cards from each color page
MAX_CARDS_PER_THEME_PAGE = 50    # top N cards from each theme page
MAX_THEME_PAGES = 350            # cap theme pages fetched per refresh
MAX_TOTAL_COMMANDERS = 700       # kept for backwards-compat; unused in new flow

# ---------------------------------------------------------------------------
# Color top-cards page slugs.
# URL pattern: https://json.edhrec.com/pages/top/<slug>.json
# Empirically verified 2026-04-23 against json.edhrec.com
# ---------------------------------------------------------------------------
COLOR_SLUGS: list[str] = [
    # Mono-color
    "white", "blue", "black", "red", "green",
    # Two-color guilds
    "azorius", "dimir", "rakdos", "gruul", "selesnya",
    "orzhov", "izzet", "golgari", "boros", "simic",
    # Three-color shards / wedges
    "esper", "grixis", "jund", "naya", "bant",
    "abzan", "jeskai", "sultai", "mardu", "temur",
    # Four-color (named by the excluded color)
    "sans-white", "sans-blue", "sans-black", "sans-red", "sans-green",
    # Five-color and colorless
    "wubrg", "colorless",
]


def _slug(name: str) -> str:
    """Convert a card name to an EDHREC URL slug."""
    s = name.lower()
    s = re.sub(r"[',]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _extract_cardlists(data: dict) -> list[dict]:
    return (data.get("container", {})
               .get("json_dict", {})
               .get("cardlists", []))


class EDHRECSource(EnrichmentSource):
    """Pull universal/archetype staples + salt scores from EDHREC.

    Archetype tier is the UNION of EDHREC's top-cards across color pages,
    theme pages, and the top-commanders page.  Cards appearing in >= 3 of
    those pages qualify.  No hardcoded archetype catalogue is maintained.
    """

    name = "edhrec"

    def __init__(self):
        self._client: Optional[httpx.Client] = None

    def probe(self) -> bool:
        from probes.probe_edhrec import probe as _probe
        r = _probe()
        return r.ok

    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        start = time.time()
        errors: list[str] = []
        warnings: list[str] = []

        if not self.probe():
            msg = "EDHREC probe failed; aborting."
            errors.append(msg)
            _record(self.name, False, msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)

        name_map = _build_name_map()

        with httpx.Client(
            timeout=TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            self._client = client

            # Step 0: global top/year for universal staples
            self._emit(emit, 0, 6, "Fetching top/year global cards …")
            top_cards = self._fetch_top_year(warnings)

            # Step 1: color top-cards pages (32 slugs)
            self._emit(emit, 1, 6,
                       f"Fetching {len(COLOR_SLUGS)} color top-cards pages …")
            color_page_cards = self._fetch_color_pages(emit, warnings)

            # Step 2: theme index + theme pages
            self._emit(emit, 2, 6, "Fetching theme index …")
            theme_slugs = self._fetch_theme_index(warnings)
            self._emit(emit, 3, 6,
                       f"Fetching {len(theme_slugs)} theme top-cards pages …")
            theme_page_cards, theme_page_slug_map = self._fetch_theme_pages(
                theme_slugs, emit, warnings)

            # Step 3: top commanders page (also captures deck_count for
            # commander_ranks table, Phase 1.10)
            self._emit(emit, 4, 6, "Fetching top-commanders page …")
            top_commander_cards = self._fetch_top_commanders_page(warnings)

            # Gather all page-card lists for archetype union.
            # Theme pages start at index len(color_page_cards) in the combined
            # list, so we shift theme_page_slug_map by that offset.
            color_offset = len(color_page_cards)
            shifted_slug_map: dict[int, str] = {
                color_offset + idx: slug
                for idx, slug in theme_page_slug_map.items()
            }

            all_archetype_pages: list[list[dict]] = (
                color_page_cards + theme_page_cards + [top_commander_cards]
            )

            # Collect unique card slugs that need detail pages (for salt)
            unique_card_slugs: set[str] = set()
            for cv in top_cards:
                if cv.get("sanitized"):
                    unique_card_slugs.add(cv["sanitized"])
            for page in all_archetype_pages:
                for cv in page:
                    slug = cv.get("sanitized") or _slug(cv.get("name", ""))
                    if slug:
                        unique_card_slugs.add(slug)

            self._emit(emit, 5, 6,
                       f"Fetching {len(unique_card_slugs)} card pages for salt …")
            card_details = self._fetch_card_pages(
                list(unique_card_slugs), emit, warnings)

            self._client = None

        self._emit(emit, 5, 6, "Computing staple tiers …")
        staple_rows, salt_rows, theme_rows, commander_rank_rows = (
            self._compute_enrichment(
                top_cards=top_cards,
                archetype_pages=all_archetype_pages,
                page_slug_map=shifted_slug_map,
                top_commander_cards=top_commander_cards,
                card_details=card_details,
                name_map=name_map,
                warnings=warnings,
            )
        )

        self._emit(emit, 6, 6, "Writing enrichment to DB …")
        rows_changed = 0
        coverage_pct = 0.0
        conn = enrichment_db.get_connection()
        try:
            rows_changed = self._write(conn, staple_rows, salt_rows,
                                       theme_rows, commander_rank_rows,
                                       warnings)
            total_staples = conn.execute(
                "SELECT COUNT(*) FROM staples WHERE source='edhrec'"
            ).fetchone()[0]
            coverage_pct = float(total_staples) / max(1, len(staple_rows)) * 100
            enrichment_db.record_sync_attempt(
                conn, self.name, success=True, coverage_pct=coverage_pct,
                version_hash=str(total_staples))
            enrichment_db.record_coverage(
                conn, self.name, key_name="universal_staples",
                expected=len([r for r in staple_rows if r["tier"] == "universal"]),
                actual=total_staples)
        except Exception as exc:
            msg = f"DB write error: {type(exc).__name__}: {exc}"
            logger.exception("EDHREC DB write error")
            errors.append(msg)
            enrichment_db.record_sync_attempt(conn, self.name, False, error=msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)
        finally:
            conn.close()

        self._emit(emit, 6, 6,
                   f"Done. {rows_changed} rows, {len(salt_rows)} salts, "
                   f"{len(theme_rows)} theme memberships, "
                   f"{len(commander_rank_rows)} commander ranks.")
        return RefreshResult(
            source=self.name, success=True,
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
            stats = conn.execute(
                """SELECT tier, COUNT(*) as cnt FROM staples
                   WHERE source='edhrec' GROUP BY tier"""
            ).fetchall()
            archetype_names = conn.execute(
                """SELECT DISTINCT archetypes_json FROM staples
                   WHERE source='edhrec' AND tier='archetype'
                   AND archetypes_json IS NOT NULL"""
            ).fetchall()
            unique_pages: set[str] = set()
            for row in archetype_names:
                try:
                    unique_pages.update(json.loads(row[0]))
                except Exception:
                    pass
            return {
                "source": self.name,
                "staples_by_tier": {r["tier"]: r["cnt"] for r in stats},
                "archetype_page_count": len(unique_pages),
                "salt_count": conn.execute(
                    "SELECT COUNT(*) FROM salt_scores").fetchone()[0],
                "theme_membership_count": conn.execute(
                    "SELECT COUNT(*) FROM themes WHERE source='edhrec'"
                ).fetchone()[0],
                "commander_rank_count": conn.execute(
                    "SELECT COUNT(*) FROM commander_ranks WHERE source='edhrec'"
                ).fetchone()[0],
                **(dict(sync) if sync else {}),
            }
        finally:
            conn.close()

    # -- Fetch helpers -------------------------------------------------------

    def _get(self, url: str) -> Optional[dict]:
        try:
            time.sleep(RATE_LIMIT_S)
            r = self._client.get(url)
            if r.status_code == 403:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.debug("EDHREC GET %s failed: %s", url, exc)
            return None

    def _fetch_top_year(self, warnings: list[str]) -> list[dict]:
        data = self._get(f"{BASE}/top/year.json")
        if not data:
            warnings.append("top/year.json returned no data (rate-limited?)")
            return []
        cardlists = _extract_cardlists(data)
        if not cardlists:
            warnings.append("top/year.json: no cardlists found")
            return []
        return cardlists[0].get("cardviews", [])

    def _fetch_color_pages(
        self,
        emit: Optional[EmitFn],
        warnings: list[str],
    ) -> list[list[dict]]:
        """Fetch all COLOR_SLUGS top-cards pages; return list-of-cardview-lists."""
        out: list[list[dict]] = []
        total = len(COLOR_SLUGS)
        for i, slug in enumerate(COLOR_SLUGS):
            if i % 8 == 0:
                self._emit(emit, 1, 6, f"Color pages: {i}/{total} …")
            data = self._get(f"{BASE}/top/{slug}.json")
            if not data:
                warnings.append(f"Color page unavailable: {slug}")
                out.append([])
                continue
            cardlists = _extract_cardlists(data)
            cards: list[dict] = []
            for lst in cardlists:
                cards.extend(lst.get("cardviews", []))
            out.append(cards[:MAX_CARDS_PER_COLOR_PAGE])
        return out

    def _fetch_theme_index(self, warnings: list[str]) -> list[str]:
        """Fetch the /themes index and return a list of theme slugs."""
        data = self._get(f"{BASE}/themes.json")
        if not data:
            warnings.append("themes.json index unavailable; skipping themes")
            return []
        jd = data.get("container", {}).get("json_dict", {})
        # EDHREC themes index may use 'themes' or 'themeLinks'
        themes = jd.get("themes") or jd.get("themeLinks") or []
        slugs: list[str] = []
        for t in themes:
            if isinstance(t, dict):
                slug = t.get("slug") or t.get("sanitized") or ""
                if slug:
                    slugs.append(slug)
            elif isinstance(t, str):
                slugs.append(t)
        return slugs[:MAX_THEME_PAGES]

    def _fetch_theme_pages(
        self,
        slugs: list[str],
        emit: Optional[EmitFn],
        warnings: list[str],
    ) -> tuple[list[list[dict]], dict[int, str]]:
        """Fetch theme top-cards pages.

        Returns:
            (page_cardview_lists, page_index_to_slug_map) where the map
            allows _compute_enrichment to associate each page index with a
            theme slug so cards can be stored in the themes table.
        """
        out: list[list[dict]] = []
        page_slug_map: dict[int, str] = {}
        total = len(slugs)
        for i, slug in enumerate(slugs):
            if i % 25 == 0:
                self._emit(emit, 3, 6, f"Theme pages: {i}/{total} …")
            data = self._get(f"{BASE}/themes/{slug}.json")
            if not data:
                warnings.append(f"Theme page unavailable: {slug}")
                out.append([])
                continue
            cardlists = _extract_cardlists(data)
            cards: list[dict] = []
            for lst in cardlists:
                cards.extend(lst.get("cardviews", []))
            page_cards = cards[:MAX_CARDS_PER_THEME_PAGE]
            page_slug_map[len(out)] = slug
            out.append(page_cards)
        return out, page_slug_map

    def _fetch_top_commanders_page(self, warnings: list[str]) -> list[dict]:
        """Fetch the top-commanders year page; return top cardviews."""
        data = self._get(f"{BASE}/top-commanders/year.json")
        if not data:
            # Fallback: try the /commanders top-level page
            data = self._get(f"{BASE}/commanders.json")
        if not data:
            warnings.append("top-commanders page unavailable")
            return []
        cardlists = _extract_cardlists(data)
        cards: list[dict] = []
        for lst in cardlists:
            cards.extend(lst.get("cardviews", []))
        return cards[:MAX_CARDS_PER_COLOR_PAGE]

    def _fetch_card_pages(
        self,
        slugs: list[str],
        emit: Optional[EmitFn],
        warnings: list[str],
    ) -> dict[str, dict]:
        out: dict[str, dict] = {}
        total = len(slugs)
        for i, slug in enumerate(slugs):
            if i % 50 == 0:
                self._emit(emit, 5, 6, f"Card pages: {i}/{total} fetched …")
            data = self._get(f"{BASE}/cards/{slug}.json")
            if not data:
                continue
            jd = data.get("container", {}).get("json_dict", {})
            card = jd.get("card")
            if isinstance(card, dict) and card.get("name"):
                out[slug] = card
        return out

    # -- Computation ---------------------------------------------------------

    def _compute_enrichment(
        self,
        top_cards: list[dict],
        archetype_pages: list[list[dict]],
        page_slug_map: dict[int, str],
        top_commander_cards: list[dict],
        card_details: dict[str, dict],
        name_map: dict[str, str],
        warnings: list[str],
    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        """Compute staple rows, salt rows, theme rows, and commander rank rows.

        Args:
            top_cards:            cardviews from the global top/year page
            archetype_pages:      list of cardview lists — one entry per
                                  color/theme/commander page.  A card qualifies
                                  for archetype tier if it appears in >=
                                  ARCHETYPE_MIN_PAGES of these.
            page_slug_map:        {page_index_in_archetype_pages: theme_slug}
                                  Only theme pages are in this map (not color
                                  pages); used to populate the themes table.
            top_commander_cards:  cardviews from the top-commanders page;
                                  num_decks stored in commander_ranks.
            card_details:         slug -> card JSON from individual card pages
                                  (for salt + universal inclusion rate)
            name_map:             card name -> oracle_id
            warnings:             accumulate non-fatal issues here

        Returns:
            (staple_rows, salt_rows, theme_rows, commander_rank_rows)
        """
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Count page appearances per oracle_id across archetype pages
        page_count: dict[str, int] = {}
        # theme_memberships: {oracle_id: set of theme_slug strings}
        theme_memberships: dict[str, set] = {}

        for page_idx, page_cards in enumerate(archetype_pages):
            theme_slug = page_slug_map.get(page_idx)
            # Deduplicate within a single page before counting
            seen_in_page: set[str] = set()
            for cv in page_cards:
                name = cv.get("name")
                if not name:
                    continue
                oid = _lookup(name, name_map)
                if not oid or oid in seen_in_page:
                    continue
                seen_in_page.add(oid)
                page_count[oid] = page_count.get(oid, 0) + 1
                # Record theme membership if this page has a slug
                if theme_slug:
                    theme_memberships.setdefault(oid, set()).add(theme_slug)

        staple_rows: list[dict] = []
        salt_rows: list[dict] = []
        seen_universal: set[str] = set()

        # Universal staples from card detail pages
        for slug, card in card_details.items():
            name = card.get("name") or (card.get("names") or [None])[0]
            if not name:
                continue
            oid = _lookup(name, name_map)
            if not oid:
                continue

            num_decks = card.get("num_decks") or 0
            potential = card.get("potential_decks") or 0
            salt = card.get("salt")

            if potential > 0 and (num_decks / potential) >= UNIVERSAL_THRESHOLD:
                if oid not in seen_universal:
                    seen_universal.add(oid)
                    staple_rows.append({
                        "oracle_id": oid,
                        "tier": "universal",
                        "source": "edhrec",
                        "score": num_decks / potential,
                        "archetypes_json": None,
                        "last_updated": ts,
                    })

            if salt is not None:
                salt_rows.append({
                    "oracle_id": oid,
                    "salt": float(salt),
                    "last_updated": ts,
                })

        # Universal from top/year (backup for cards without detail pages)
        for cv in top_cards:
            name = cv.get("name")
            if not name:
                continue
            oid = _lookup(name, name_map)
            if not oid or oid in seen_universal:
                continue
            num_decks = cv.get("num_decks") or 0
            potential = cv.get("potential_decks") or 0
            if potential > 0 and (num_decks / potential) >= UNIVERSAL_THRESHOLD:
                seen_universal.add(oid)
                staple_rows.append({
                    "oracle_id": oid,
                    "tier": "universal",
                    "source": "edhrec",
                    "score": num_decks / potential,
                    "archetypes_json": None,
                    "last_updated": ts,
                })

        # Archetype staples: appear in >= ARCHETYPE_MIN_PAGES pages
        for oid, count in page_count.items():
            if count >= ARCHETYPE_MIN_PAGES:
                # score = fraction of pages the card appeared in
                score = count / max(1, len(archetype_pages))
                staple_rows.append({
                    "oracle_id": oid,
                    "tier": "archetype",
                    "source": "edhrec",
                    "score": score,
                    # archetypes_json stores a JSON int (page count) for
                    # introspection; not a named list since we use page union now.
                    "archetypes_json": json.dumps(count),
                    "last_updated": ts,
                })

        # Theme membership rows (Phase 1.10)
        theme_rows: list[dict] = []
        for oid, slugs in theme_memberships.items():
            for theme_slug in slugs:
                theme_rows.append({
                    "oracle_id": oid,
                    "theme_name": theme_slug,
                    "source": "edhrec",
                    "inclusion_pct": None,
                    "last_updated": ts,
                })

        # Commander popularity rows (Phase 1.10)
        # top_commander_cards contains commanders with their deck counts.
        commander_rank_rows: list[dict] = []
        seen_commanders: set[str] = set()
        for cv in top_commander_cards:
            name = cv.get("name")
            if not name:
                continue
            oid = _lookup(name, name_map)
            if not oid or oid in seen_commanders:
                continue
            num_decks = cv.get("num_decks") or cv.get("deck_count") or 0
            if num_decks > 0:
                seen_commanders.add(oid)
                commander_rank_rows.append({
                    "oracle_id": oid,
                    "deck_count": num_decks,
                    "avg_synergy_json": None,
                    "source": "edhrec",
                    "last_updated": ts,
                })

        return staple_rows, salt_rows, theme_rows, commander_rank_rows

    # -- DB write ------------------------------------------------------------

    @staticmethod
    def _write(
        conn,
        staple_rows: list[dict],
        salt_rows: list[dict],
        theme_rows: list[dict],
        commander_rank_rows: list[dict],
        warnings: list[str],
    ) -> int:
        """Write all enrichment rows atomically.

        Writes staples, salt_scores, themes, and commander_ranks in a single
        transaction.  Any error rolls back the entire write so partial data
        is never committed.
        """
        changed = 0
        with conn:
            conn.executemany(
                """INSERT INTO staples
                       (oracle_id, tier, source, score,
                        archetypes_json, last_updated)
                   VALUES (:oracle_id, :tier, :source, :score,
                           :archetypes_json, :last_updated)
                   ON CONFLICT(oracle_id, tier, source) DO UPDATE SET
                       score = excluded.score,
                       archetypes_json = excluded.archetypes_json,
                       last_updated = excluded.last_updated""",
                staple_rows,
            )
            changed += len(staple_rows)

            conn.executemany(
                """INSERT INTO salt_scores (oracle_id, salt, last_updated)
                   VALUES (:oracle_id, :salt, :last_updated)
                   ON CONFLICT(oracle_id) DO UPDATE SET
                       salt = excluded.salt,
                       last_updated = excluded.last_updated""",
                salt_rows,
            )
            changed += len(salt_rows)

            # Theme membership rows (Phase 1.10)
            if theme_rows:
                conn.executemany(
                    """INSERT INTO themes
                           (oracle_id, theme_name, source, inclusion_pct)
                       VALUES (:oracle_id, :theme_name, :source, :inclusion_pct)
                       ON CONFLICT(oracle_id, theme_name, source) DO UPDATE SET
                           inclusion_pct = excluded.inclusion_pct""",
                    theme_rows,
                )
                changed += len(theme_rows)

            # Commander popularity rows (Phase 1.10)
            if commander_rank_rows:
                conn.executemany(
                    """INSERT INTO commander_ranks
                           (oracle_id, deck_count, avg_synergy_json,
                            source, last_updated)
                       VALUES (:oracle_id, :deck_count, :avg_synergy_json,
                               :source, :last_updated)
                       ON CONFLICT(oracle_id) DO UPDATE SET
                           deck_count = excluded.deck_count,
                           avg_synergy_json = excluded.avg_synergy_json,
                           last_updated = excluded.last_updated""",
                    commander_rank_rows,
                )
                changed += len(commander_rank_rows)

        return changed

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _emit(emit: Optional[EmitFn], step: int, total: int,
              message: str) -> None:
        if emit is None:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            emit("enrichment_refresh_progress", {
                "source": "edhrec",
                "step": step, "total": total,
                "message": message, "ts": ts,
            })
        except Exception:
            pass


def _build_name_map() -> dict[str, str]:
    """Load name → oracle_id from card_universe."""
    conn = enrichment_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT name, oracle_id FROM card_universe").fetchall()
        return {r["name"]: r["oracle_id"] for r in rows}
    finally:
        conn.close()


def _lookup(name: str, name_map: dict[str, str]) -> Optional[str]:
    return name_map.get(name)


def _record(source: str, success: bool, error: Optional[str] = None) -> None:
    conn = enrichment_db.get_connection()
    try:
        enrichment_db.record_sync_attempt(conn, source, success, error=error)
    finally:
        conn.close()


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)
