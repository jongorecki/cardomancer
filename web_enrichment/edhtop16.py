# web_enrichment/edhtop16.py
# ---------------------------------------------------------------------------
# EDHTop16Source: pulls cEDH staples from edhtop16.com/api/graphql.
#
# The `staples` GraphQL query returns a flat list of cards that appear in
# the cEDH meta (based on top-16 tournament finishes over the past year).
# Each card has `oracleId` and `playRateLastYear` (0–1 float).
#
# Threshold: playRateLastYear >= 0.15 → staple:cedh tier.
# Basic lands are explicitly excluded by type check.
#
# The endpoint requires no authentication. It is a public GraphQL API
# intended for external query (edhtop16.com runs it for their own UI).
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
TIMEOUT_S = 45
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"

# Threshold for cedh staple tier (15% appearance in top-16 decklists)
CEDH_THRESHOLD = 0.15

# Card type strings that indicate basic lands — excluded from staples.
_BASIC_LAND_TYPES = frozenset({
    "Basic Land", "Basic Snow Land",
    "Basic Land \u2014 Plains", "Basic Land \u2014 Island",
    "Basic Land \u2014 Swamp", "Basic Land \u2014 Mountain",
    "Basic Land \u2014 Forest",
})

STAPLES_QUERY = """
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
    """Pull cEDH staple data from edhtop16.com."""

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

        _emit_progress(emit, 0, 3, "Querying edhtop16 staples …")

        try:
            staples = self._fetch_staples()
        except Exception as exc:
            msg = f"GraphQL fetch failed: {type(exc).__name__}: {exc}"
            logger.exception("edhtop16 fetch error")
            errors.append(msg)
            _record(self.name, False, msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)

        _emit_progress(emit, 1, 3,
                       f"Processing {len(staples)} staple candidates …")

        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        rows: list[dict] = []
        skipped_land = 0
        skipped_threshold = 0

        for card in staples:
            oracle_id = card.get("oracleId")
            if not oracle_id:
                continue

            # Exclude basic lands
            card_type = (card.get("type") or "").strip()
            if _is_basic_land(card_type):
                skipped_land += 1
                continue

            rate = card.get("playRateLastYear") or 0.0
            if rate < CEDH_THRESHOLD:
                skipped_threshold += 1
                continue

            rows.append({
                "oracle_id": oracle_id,
                "tier": "cedh",
                "source": "edhtop16",
                "score": float(rate),
                "archetypes_json": None,
                "last_updated": ts,
            })

        if skipped_land:
            warnings.append(
                f"Excluded {skipped_land} basic lands from cEDH staples.")
        if skipped_threshold:
            warnings.append(
                f"Excluded {skipped_threshold} cards below {CEDH_THRESHOLD:.0%} threshold.")

        _emit_progress(emit, 2, 3,
                       f"Writing {len(rows)} cEDH staples to DB …")

        conn = enrichment_db.get_connection()
        rows_changed = 0
        try:
            rows_changed = self._write(conn, rows)
            total = conn.execute(
                "SELECT COUNT(*) FROM staples WHERE source='edhtop16'"
            ).fetchone()[0]
            coverage_pct = float(total) / max(1, len(staples)) * 100
            enrichment_db.record_sync_attempt(
                conn, self.name, success=True,
                coverage_pct=coverage_pct,
                version_hash=str(len(staples)),
            )
            enrichment_db.record_coverage(
                conn, self.name, key_name="cedh_staples",
                expected=len(rows), actual=total,
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

        _emit_progress(emit, 3, 3,
                       f"Done. {rows_changed} cEDH staple rows written.")
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
                "SELECT COUNT(*) FROM staples WHERE source='edhtop16' AND tier='cedh'"
            ).fetchone()[0]
            return {
                "source": self.name,
                "cedh_staple_count": cedh_count,
                **(dict(sync) if sync else {}),
            }
        finally:
            conn.close()

    # -- Internals -----------------------------------------------------------

    @staticmethod
    def _fetch_staples() -> list[dict]:
        r = httpx.post(
            ENDPOINT,
            json={"query": STAPLES_QUERY},
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
