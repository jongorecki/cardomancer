# web_enrichment/edhtop16.py
# ---------------------------------------------------------------------------
# EDHTop16Source: pulls cEDH staples from edhtop16.com/api/graphql using a
# 6-month rolling window of actual tournament top-16 decklists.
#
# Approach (2026-04-20):
#   1. Query tournaments(filters: {timePeriod: SIX_MONTHS, minSize: 16})
#      paginated in batches of 50. Cap at MAX_TOURNAMENTS tournaments.
#   2. For each tournament, fetch top-16 entries (maxStanding: 16) with their
#      maindeck (oracleId + type).
#   3. For each card, count how many distinct top-16 decks included it.
#   4. play_rate = deck_count / total_top16_entries; cards above
#      CEDH_THRESHOLD (15%) → staple:cedh tier.
#   5. Basic lands excluded by type-line check.
#
# Fallback: if tournament deck data returns empty, falls back to the
# `staples` query which provides playRateLastYear (annual).
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

ENDPOINT = "https://edhtop16.com/api/graphql"
TIMEOUT_S = 60
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"

CEDH_THRESHOLD = 0.15        # 15% appearance rate → staple:cedh
MAX_TOURNAMENTS = 500        # cap on tournaments fetched per refresh
MIN_TOURNAMENT_SIZE = 16     # minSize filter for tournaments query
TOP_CUT = 16                 # maxStanding for entries per tournament
PAGE_SIZE = 50               # tournaments per GraphQL page
RATE_LIMIT_BACKOFF_S = 2.0   # initial sleep on 429 responses
RATE_LIMIT_MAX_RETRIES = 3   # max retry attempts per page on 429

SIX_MONTH_QUERY = """
query ($after: String) {
  tournaments(
    first: %(page_size)d,
    after: $after,
    filters: {timePeriod: SIX_MONTHS, minSize: %(min_size)d}
  ) {
    edges {
      node {
        TID
        entries(maxStanding: %(top_cut)d) {
          maindeck {
            oracleId
            type
          }
        }
      }
    }
    pageInfo { hasNextPage endCursor }
  }
}
""" % {"page_size": PAGE_SIZE, "min_size": MIN_TOURNAMENT_SIZE,
       "top_cut": TOP_CUT}

# Fallback: annual staples when tournament data is unavailable
STAPLES_FALLBACK_QUERY = """
query {
  staples {
    name
    oracleId
    colorId
    type
    playRateLastYear
  }
}
"""


class EDHTop16Source(EnrichmentSource):
    """Pull cEDH staples from edhtop16.com using 6-month tournament data."""

    name = "edhtop16"

    def probe(self) -> bool:
        from probes.probe_edhtop16 import probe as _probe
        r = _probe()
        return r.ok

    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        start = time.time()
        errors: list[str] = []
        warnings: list[str] = []

        if not self.probe():
            msg = "edhtop16 probe failed; aborting."
            errors.append(msg)
            _record(self.name, False, msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)

        _emit_progress(emit, 0, 4,
                       "Fetching 6-month tournament decklists …")

        try:
            card_counts, total_entries = self._fetch_tournament_decks(
                emit, warnings)
        except Exception as exc:
            msg = f"Tournament fetch failed: {type(exc).__name__}: {exc}"
            logger.exception("edhtop16 tournament fetch error")
            errors.append(msg)
            _record(self.name, False, msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)

        # Fallback to annual staples if tournament data came back empty
        if not card_counts or total_entries == 0:
            warnings.append(
                "Tournament deck data empty; falling back to playRateLastYear.")
            try:
                staples = self._fetch_staples()
                card_counts = {}
                total_entries = 1  # denominator placeholder
                for s in staples:
                    oid = s.get("oracleId")
                    rate = s.get("playRateLastYear") or 0.0
                    type_line = s.get("type") or ""
                    if oid and rate >= CEDH_THRESHOLD and not _is_basic_land(type_line):
                        # Store synthetic count so downstream math works
                        card_counts[oid] = int(rate * 10000)
                total_entries = 10000
            except Exception as exc2:
                msg = f"Fallback staples fetch failed: {exc2}"
                errors.append(msg)
                _record(self.name, False, msg)
                return RefreshResult(source=self.name, success=False,
                                     duration_ms=_ms(start), errors=errors)

        _emit_progress(emit, 2, 4,
                       f"Computing staples from {total_entries} decks …")

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows: list[dict] = []
        skipped_threshold = 0

        for oracle_id, count in card_counts.items():
            rate = count / max(1, total_entries)
            if rate < CEDH_THRESHOLD:
                skipped_threshold += 1
                continue
            rows.append({
                "oracle_id": oracle_id,
                "tier": "cedh",
                "source": "edhtop16",
                "score": round(rate, 6),
                "archetypes_json": None,
                "last_updated": ts,
            })

        if skipped_threshold:
            warnings.append(
                f"Excluded {skipped_threshold} cards below "
                f"{CEDH_THRESHOLD:.0%} threshold.")

        _emit_progress(emit, 3, 4,
                       f"Writing {len(rows)} cEDH staples to DB …")

        # Global rankings for the `edhtop16-top:N` query predicate.
        # Sourced from the same card_counts as the staples write — every
        # card the source saw, ranked by inclusion%, regardless of
        # whether it crossed the staple threshold.
        rank_rows = self._compute_rankings(card_counts, total_entries)

        conn = enrichment_db.get_connection()
        rows_changed = 0
        coverage_pct = 0.0
        try:
            rows_changed = self._write(conn, rows)
            rows_changed += self._write_rankings(conn, rank_rows)
            total_stored = conn.execute(
                "SELECT COUNT(*) FROM staples WHERE source='edhtop16'"
            ).fetchone()[0]
            coverage_pct = float(total_stored) / max(1, len(rows)) * 100
            enrichment_db.record_sync_attempt(
                conn, self.name, success=True,
                coverage_pct=coverage_pct,
                version_hash=f"{len(rows)}:{total_entries}",
            )
            enrichment_db.record_coverage(
                conn, self.name, key_name="cedh_staples",
                expected=len(rows), actual=total_stored,
            )
        except Exception as exc:
            msg = f"DB write error: {type(exc).__name__}: {exc}"
            logger.exception("edhtop16 DB write error")
            errors.append(msg)
            enrichment_db.record_sync_attempt(conn, self.name, False, error=msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)
        finally:
            conn.close()

        _emit_progress(emit, 4, 4,
                       f"Done. {rows_changed} cEDH staple rows, "
                       f"{total_entries} decks sampled.")
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
            cedh_count = conn.execute(
                "SELECT COUNT(*) FROM staples "
                "WHERE source='edhtop16' AND tier='cedh'"
            ).fetchone()[0]
            return {
                "source": self.name,
                "cedh_staple_count": cedh_count,
                **(dict(sync) if sync else {}),
            }
        finally:
            conn.close()

    # -- Internals -----------------------------------------------------------

    def _fetch_tournament_decks(
        self,
        emit: Optional[EmitFn],
        warnings: list[str],
    ) -> tuple[dict[str, int], int]:
        """Paginate 6-month tournament top-16 decklists.

        Returns ({oracle_id: deck_count}, total_entries) where deck_count is
        the number of distinct top-16 decks that included each card.
        """
        card_counts: dict[str, int] = {}
        total_entries = 0
        processed = 0
        cursor: Optional[str] = None

        with httpx.Client(
            timeout=TIMEOUT_S,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        ) as client:
            while processed < MAX_TOURNAMENTS:
                variables: dict = {}
                if cursor:
                    variables["after"] = cursor

                data = None
                for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
                    try:
                        r = client.post(
                            ENDPOINT,
                            json={"query": SIX_MONTH_QUERY,
                                  "variables": variables},
                        )
                        if r.status_code == 429:
                            backoff = RATE_LIMIT_BACKOFF_S * (2 ** attempt)
                            warnings.append(
                                f"Rate-limited (429) on page "
                                f"processed={processed}; "
                                f"sleeping {backoff:.0f}s …")
                            logger.warning(
                                "edhtop16: 429 on page %d, attempt %d/%d, "
                                "backoff %.0fs",
                                processed, attempt + 1,
                                RATE_LIMIT_MAX_RETRIES, backoff)
                            time.sleep(backoff)
                            continue
                        r.raise_for_status()
                        data = r.json()
                        break
                    except httpx.HTTPStatusError:
                        raise
                    except Exception as exc:
                        warnings.append(
                            f"Tournament page fetch error "
                            f"(processed={processed}, attempt={attempt}): {exc}")
                        break

                if data is None:
                    break

                if "errors" in data:
                    warnings.append(
                        f"GraphQL errors: {data['errors'][:1]}")
                    break

                conn_data = (data.get("data") or {}).get("tournaments", {})
                edges = conn_data.get("edges") or []

                for edge in edges:
                    if processed >= MAX_TOURNAMENTS:
                        break
                    node = edge.get("node", {})
                    processed += 1
                    entries = node.get("entries") or []

                    for entry in entries:
                        maindeck = entry.get("maindeck") or []
                        if not maindeck:
                            continue
                        total_entries += 1
                        seen_in_deck: set[str] = set()
                        for card in maindeck:
                            oid = card.get("oracleId")
                            type_line = card.get("type") or ""
                            if not oid:
                                continue
                            if _is_basic_land(type_line):
                                continue
                            if oid not in seen_in_deck:
                                seen_in_deck.add(oid)
                                card_counts[oid] = card_counts.get(oid, 0) + 1

                if processed % 100 == 0 and processed > 0:
                    _emit_progress(
                        emit, 1, 4,
                        f"Processed {processed} tournaments, "
                        f"{total_entries} top-16 decks …")

                page_info = conn_data.get("pageInfo", {})
                if not page_info.get("hasNextPage") or not edges:
                    break
                cursor = page_info.get("endCursor")

        logger.info(
            "edhtop16: %d tournaments, %d top-16 decks, %d unique cards",
            processed, total_entries, len(card_counts),
        )
        return card_counts, total_entries

    @staticmethod
    def _fetch_staples() -> list[dict]:
        """Fallback: annual staples from the edhtop16 `staples` query."""
        r = httpx.post(
            ENDPOINT,
            json={"query": STAPLES_FALLBACK_QUERY},
            timeout=TIMEOUT_S,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        r.raise_for_status()
        data = r.json()
        if "errors" in data:
            raise RuntimeError(f"GraphQL errors: {data['errors']}")
        return (data.get("data") or {}).get("staples") or []

    @staticmethod
    def _write(conn, rows: list[dict]) -> int:
        with conn:
            conn.executemany(
                """INSERT INTO staples
                       (oracle_id, tier, source, score,
                        archetypes_json, last_updated)
                   VALUES (:oracle_id, :tier, :source, :score,
                           :archetypes_json, :last_updated)
                   ON CONFLICT(oracle_id, tier, source) DO UPDATE SET
                       score = excluded.score,
                       last_updated = excluded.last_updated""",
                rows,
            )
        return len(rows)

    # -- Global rankings (Phase 3 follow-on, edhtop16-top:N predicate) -------

    @staticmethod
    def _compute_rankings(card_counts: dict[str, int],
                          total_entries: int) -> list[dict]:
        """Build a global cEDH ranking from per-card tournament-deck counts.

        score = deck_count / total_entries (the inclusion percentage in
        the six-month tournament corpus). Sort DESC, deterministic
        tiebreak on oracle_id, assign ranks 1..N.

        Cards seen in zero tournament decks aren't ranked — same
        contract as edhrec-top.
        """
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        denom = max(1, int(total_entries or 0))
        scored = []
        for oid, count in card_counts.items():
            if not oid or not count:
                continue
            scored.append((oid, count / denom))
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return [
            {
                "oracle_id": oid,
                "source": "edhtop16",
                "rank": rank,
                "score": float(score),
                "last_updated": ts,
            }
            for rank, (oid, score) in enumerate(scored, start=1)
        ]

    @staticmethod
    def _write_rankings(conn, rank_rows: list[dict]) -> int:
        """Atomically replace this source's rank rows. Other sources'
        rows (notably edhrec) stay untouched."""
        if not rank_rows:
            return 0
        with conn:
            conn.execute(
                "DELETE FROM card_rankings WHERE source = 'edhtop16'"
            )
            conn.executemany(
                """INSERT INTO card_rankings
                       (oracle_id, source, rank, score, last_updated)
                   VALUES (:oracle_id, :source, :rank, :score,
                           :last_updated)""",
                rank_rows,
            )
        return len(rank_rows)


def _is_basic_land(type_line: str) -> bool:
    """Return True if the card type indicates a basic land."""
    tl = type_line.lower()
    return "basic land" in tl or "basic snow land" in tl


def _emit_progress(emit: Optional[EmitFn], step: int, total: int,
                   message: str) -> None:
    if emit is None:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        emit("enrichment_refresh_progress", {
            "source": "edhtop16",
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
