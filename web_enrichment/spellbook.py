# web_enrichment/spellbook.py
# ---------------------------------------------------------------------------
# SpellbookSource: pulls combos from backend.commanderspellbook.com and
# writes them to the `combos` + `combo_membership` tables in enrichment.db.
#
# API:  GET /variants/?limit=100&offset=N
# Auth: none required (public read)
# Rate: not documented; we use 100ms between pages (conservative).
#
# Key shape differences from the original plan doc (confirmed by probe):
#   - `count` is always null; paginate by following `next` URL until null.
#   - `uses[].card.oracleId` (camelCase), not `oracleCardId`.
#   - `produces[].feature.name` is the combo result text.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

import enrichment_db
from web_enrichment.base import EmitFn, EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)

BASE_URL = "https://backend.commanderspellbook.com/variants/"
PAGE_SIZE = 100
RATE_LIMIT_S = 0.10
TIMEOUT_S = 30
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"


class SpellbookSource(EnrichmentSource):
    """Pull all combos from Commander Spellbook into enrichment.db."""

    name = "spellbook"

    def probe(self) -> bool:
        from probes.probe_spellbook import probe as _probe
        r = _probe()
        return r.ok

    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        start = time.time()
        errors: list[str] = []
        warnings: list[str] = []

        if not self.probe():
            msg = "Spellbook probe failed; aborting refresh."
            logger.warning(msg)
            errors.append(msg)
            conn = enrichment_db.get_connection()
            try:
                enrichment_db.record_sync_attempt(
                    conn, self.name, success=False, error=msg, coverage_pct=0.0)
            finally:
                conn.close()
            return RefreshResult(
                source=self.name, success=False,
                duration_ms=int((time.time() - start) * 1000),
                errors=errors,
            )

        self._emit_progress(emit, step=0, total=1,
                            message="Fetching combos from Commander Spellbook …")

        try:
            combos, members = self._fetch_all(emit)
        except Exception as exc:
            msg = f"Fetch failed: {type(exc).__name__}: {exc}"
            logger.exception("Spellbook fetch error")
            errors.append(msg)
            conn = enrichment_db.get_connection()
            try:
                enrichment_db.record_sync_attempt(
                    conn, self.name, success=False, error=msg, coverage_pct=0.0)
            finally:
                conn.close()
            return RefreshResult(
                source=self.name, success=False,
                duration_ms=int((time.time() - start) * 1000),
                errors=errors,
            )

        self._emit_progress(emit, step=1, total=2,
                            message=f"Writing {len(combos)} combos to DB …")

        conn = enrichment_db.get_connection()
        rows_changed = 0
        try:
            rows_changed = self._write(conn, combos, members)
            total_combos = conn.execute(
                "SELECT COUNT(*) FROM combos").fetchone()[0]
            coverage_pct = 100.0  # we pull everything; no partial coverage
            enrichment_db.record_sync_attempt(
                conn, self.name, success=True,
                coverage_pct=coverage_pct,
                version_hash=str(len(combos)),
            )
            enrichment_db.record_coverage(
                conn, self.name, key_name="combos",
                expected=len(combos), actual=total_combos,
            )
        except Exception as exc:
            msg = f"DB write error: {type(exc).__name__}: {exc}"
            logger.exception("Spellbook DB write error")
            errors.append(msg)
            enrichment_db.record_sync_attempt(
                conn, self.name, success=False, error=msg)
            return RefreshResult(
                source=self.name, success=False,
                duration_ms=int((time.time() - start) * 1000),
                errors=errors,
            )
        finally:
            conn.close()

        duration_ms = int((time.time() - start) * 1000)
        self._emit_progress(emit, step=2, total=2,
                            message=f"Done. {len(combos)} combos in DB.")
        return RefreshResult(
            source=self.name,
            success=True,
            duration_ms=duration_ms,
            rows_changed=rows_changed,
            coverage_pct=100.0,
            warnings=warnings,
        )

    def coverage_report(self) -> dict:
        conn = enrichment_db.get_connection()
        try:
            sync = conn.execute(
                "SELECT * FROM sync_metadata WHERE source = ?",
                (self.name,),
            ).fetchone()
            combo_count = conn.execute(
                "SELECT COUNT(*) FROM combos WHERE source = 'spellbook'"
            ).fetchone()[0]
            member_count = conn.execute(
                "SELECT COUNT(*) FROM combo_membership"
            ).fetchone()[0]
            return {
                "source": self.name,
                "combo_count": combo_count,
                "member_count": member_count,
                **(dict(sync) if sync else {}),
            }
        finally:
            conn.close()

    # -- Internals -----------------------------------------------------------

    def _fetch_all(
        self, emit: Optional[EmitFn]
    ) -> tuple[list[dict], list[dict]]:
        """Return (combo_rows, member_rows) pulled from all pages."""
        combos: list[dict] = []
        members: list[dict] = []
        url: Optional[str] = BASE_URL
        params: dict = {"limit": PAGE_SIZE}
        page = 0
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        with httpx.Client(
            timeout=TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            while url:
                if page > 0:
                    time.sleep(RATE_LIMIT_S)

                if page == 0:
                    resp = client.get(url, params=params)
                else:
                    resp = client.get(url)
                resp.raise_for_status()
                data = resp.json()

                results = data.get("results") or []
                for item in results:
                    combo_id = item.get("id")
                    if not combo_id:
                        continue

                    produces = item.get("produces") or []
                    result_parts = []
                    for p in produces:
                        feat = p.get("feature") or {}
                        name = feat.get("name") or ""
                        if name:
                            result_parts.append(name)
                    result_text = "; ".join(result_parts) if result_parts else None

                    prereqs_raw = item.get("easyPrerequisites") or []
                    if not prereqs_raw:
                        prereqs_raw = item.get("notablePrerequisites") or []

                    combos.append({
                        "combo_id": str(combo_id),
                        "result": result_text,
                        "identity": item.get("identity"),
                        "mana_needed": item.get("manaNeeded"),
                        "prerequisites_json": json.dumps(prereqs_raw),
                        "source": "spellbook",
                    })

                    uses = item.get("uses") or []
                    for use in uses:
                        card = use.get("card") or {}
                        oracle_id = card.get("oracleId")
                        if oracle_id:
                            members.append({
                                "oracle_id": oracle_id,
                                "combo_id": str(combo_id),
                                "quantity": use.get("quantity") or 1,
                            })

                page += 1
                self._emit_progress(
                    emit,
                    step=page,
                    total=page + 1,  # unknown total; use indeterminate progress
                    message=f"Page {page}: {len(combos)} combos so far …",
                )

                url = data.get("next")

        return combos, members

    @staticmethod
    def _write(conn, combos: list[dict], members: list[dict]) -> int:
        """Upsert combos + membership in one transaction. Returns rows changed."""
        changed = 0
        with conn:
            conn.executemany(
                """INSERT INTO combos
                       (combo_id, result, identity, mana_needed,
                        prerequisites_json, source)
                   VALUES (:combo_id, :result, :identity, :mana_needed,
                           :prerequisites_json, :source)
                   ON CONFLICT(combo_id) DO UPDATE SET
                       result = excluded.result,
                       identity = excluded.identity,
                       mana_needed = excluded.mana_needed,
                       prerequisites_json = excluded.prerequisites_json""",
                combos,
            )
            changed += len(combos)

            conn.executemany(
                """INSERT INTO combo_membership
                       (oracle_id, combo_id, quantity)
                   VALUES (:oracle_id, :combo_id, :quantity)
                   ON CONFLICT(oracle_id, combo_id) DO UPDATE SET
                       quantity = excluded.quantity""",
                members,
            )
            changed += len(members)
        return changed

    @staticmethod
    def _emit_progress(
        emit: Optional[EmitFn],
        step: int,
        total: int,
        message: str,
    ) -> None:
        if emit is None:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            emit("enrichment_refresh_progress", {
                "source": "spellbook",
                "step": step,
                "total": total,
                "message": message,
                "ts": ts,
            })
        except Exception:
            pass
