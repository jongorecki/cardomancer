# web_enrichment/buylist_ck.py
# ---------------------------------------------------------------------------
# CardKingdomBuylistSource: daily-refreshed CK buylist prices into the
# `buylists` table (vendor='ck').
#
# Scraping strategy (chosen after probing):
#   CK exposes a JSON product API used by their site front-end:
#     GET https://www.cardkingdom.com/api/json?filter[buylist]=1&page=N
#   Each page returns a JSON payload with:
#     { "data": [ { "name": ..., "setName": ..., "setCode": ...,
#                   "buyPrice": ... }, ... ],
#       "meta": { "current_page": N, "last_page": M } }
#
#   This is much more reliable than scraping the HTML listing pages
#   (which paginate across ~200+ set-category sub-pages with inconsistent
#   markup). The API is not officially documented but is used by every
#   page load of the buylist UI.
#
# Name resolution:
#   Primary:   (name, set_code) -> oracle_id via cards.CARD_DATA_BY_ID index
#   Fallback:  name-only -> oracle_id (unique names only)
#   Unresolved cards land in RefreshResult.warnings (capped at 50).
#
# Transaction safety:
#   All buylists rows for vendor='ck' are replaced atomically:
#     DELETE WHERE vendor='ck', then INSERT new rows, inside one transaction.
#   If anything raises mid-write the transaction rolls back and no rows change.
#
# Rate limiting: 1 req/s between API pages.
# Max wall time: ~10 min; emits progress every 10 pages.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
from bs4 import BeautifulSoup

import enrichment_db
from web_enrichment.base import EmitFn, EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CK_API_URL = "https://www.cardkingdom.com/api/json"
CK_BUYLIST_PAGE_URL = "https://www.cardkingdom.com/purchasing/mtg_singles"
RATE_LIMIT_S = 1.0          # 1 req/sec
TIMEOUT_S = 30
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"
MAX_WARNINGS = 50           # cap unresolved-name warnings to avoid log flood
VENDOR = "ck"

# Coverage: warn if the new row count differs from yesterday by > 10%
COVERAGE_DELTA_WARN_PCT = 0.10


class CardKingdomBuylistSource(EnrichmentSource):
    """Daily CK buylist refresh. Populates buylists(vendor='ck') rows."""

    name = "buylist_ck"

    def probe(self) -> bool:
        """Delegate to probes.probe_buylist_ck.probe()."""
        from probes.probe_buylist_ck import probe as _probe
        r = _probe()
        return r.ok

    # -----------------------------------------------------------------------

    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        start = time.time()
        errors: list[str] = []
        warnings: list[str] = []

        # 1. Short-circuit if probe fails
        if not self.probe():
            msg = "CardKingdom buylist probe failed; aborting refresh."
            logger.warning(msg)
            errors.append(msg)
            _record_sync(self.name, success=False, error=msg)
            return RefreshResult(
                source=self.name, success=False,
                duration_ms=_ms(start), errors=errors,
            )

        _emit_progress(emit, 0, 1, "Fetching CK buylist …", self.name)

        # 2. Build name-resolution index from local cards data
        name_index, name_set_index = _build_name_index()

        # 3. Fetch all rows
        try:
            raw_rows = self._fetch_all_rows(emit, warnings)
        except Exception as exc:
            msg = f"Fetch failed: {type(exc).__name__}: {exc}"
            logger.exception("CK buylist fetch error")
            errors.append(msg)
            _record_sync(self.name, success=False, error=msg)
            return RefreshResult(
                source=self.name, success=False,
                duration_ms=_ms(start), errors=errors, warnings=warnings,
            )

        if not raw_rows:
            msg = ("Fetch returned zero rows — aborting to avoid wiping "
                   "existing data.")
            logger.warning(msg)
            warnings.append(msg)
            _record_sync(self.name, success=False, error=msg)
            return RefreshResult(
                source=self.name, success=False,
                duration_ms=_ms(start), errors=errors, warnings=warnings,
            )

        # Sentinel: track raw row count so we can detect total-unresolvable case
        _raw_row_count = len(raw_rows)

        # 4. Resolve names -> oracle_ids
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        buylist_rows: list[dict] = []
        unresolved = 0

        for row in raw_rows:
            name = (row.get("name") or "").strip()
            set_code = (row.get("set_code") or "").strip().lower()
            raw_price = row.get("price")

            if not name:
                continue

            price = _parse_price(raw_price)
            if price is None:
                if len(warnings) < MAX_WARNINGS:
                    warnings.append(
                        f"Skipping row with unparseable price: "
                        f"{name!r} price={raw_price!r}")
                continue

            oracle_id = _resolve_oracle_id(
                name, set_code, name_index, name_set_index)
            if oracle_id is None:
                unresolved += 1
                if unresolved <= MAX_WARNINGS:
                    warnings.append(f"Unresolved name: {name!r} ({set_code})")
                continue

            buylist_rows.append({
                "oracle_id": oracle_id,
                "vendor": VENDOR,
                "price_usd": price,
                "last_updated": ts,
            })

        if unresolved > MAX_WARNINGS:
            warnings.append(
                f"... and {unresolved - MAX_WARNINGS} more unresolved names "
                f"(total unresolved: {unresolved})")

        # Guard: if we had non-empty raw_rows but resolved zero, abort to
        # avoid silently wiping the vendor's existing rows.
        if not buylist_rows:
            msg = (f"All {_raw_row_count} fetched rows failed name resolution "
                   "— aborting to avoid wiping existing data.")
            logger.warning(msg)
            warnings.append(msg)
            _record_sync(self.name, success=False, error=msg)
            return RefreshResult(
                source=self.name, success=False,
                duration_ms=_ms(start), errors=errors, warnings=warnings,
            )

        _emit_progress(
            emit, 1, 2,
            f"Resolved {len(buylist_rows)} rows; writing to DB …",
            self.name)

        # 5. Atomic write
        conn = enrichment_db.get_connection()
        rows_changed = 0
        try:
            rows_changed, prev_count = self._write(conn, buylist_rows)
            new_count = len(buylist_rows)
            coverage_pct = _compute_coverage_pct(new_count)

            # 6. Coverage delta check
            if prev_count > 0:
                delta = abs(new_count - prev_count) / prev_count
                if delta > COVERAGE_DELTA_WARN_PCT:
                    warnings.append(
                        f"Row count changed by {delta:.0%}: "
                        f"{prev_count} -> {new_count}. "
                        "Possible data quality issue.")

            enrichment_db.record_sync_attempt(
                conn, self.name, success=True,
                coverage_pct=coverage_pct,
                version_hash=str(new_count),
            )
            enrichment_db.record_coverage(
                conn, self.name, key_name="buylist_rows",
                expected=prev_count or new_count,
                actual=new_count,
            )
        except Exception as exc:
            msg = f"DB write error: {type(exc).__name__}: {exc}"
            logger.exception("CK buylist DB write error")
            errors.append(msg)
            try:
                enrichment_db.record_sync_attempt(
                    conn, self.name, success=False, error=msg)
            except Exception:
                pass
            return RefreshResult(
                source=self.name, success=False,
                duration_ms=_ms(start), errors=errors, warnings=warnings,
            )
        finally:
            conn.close()

        duration_ms = _ms(start)
        _emit_progress(
            emit, 2, 2,
            f"Done. {len(buylist_rows)} CK buylist rows written.",
            self.name)
        return RefreshResult(
            source=self.name, success=True,
            duration_ms=duration_ms,
            rows_changed=rows_changed,
            coverage_pct=coverage_pct,
            warnings=warnings,
        )

    # -----------------------------------------------------------------------

    def coverage_report(self) -> dict:
        """Read-only summary from enrichment.db. No network. < 100ms."""
        conn = enrichment_db.get_connection()
        try:
            sync = conn.execute(
                "SELECT * FROM sync_metadata WHERE source = ?",
                (self.name,),
            ).fetchone()
            row = conn.execute(
                "SELECT COUNT(*) as cnt, MAX(last_updated) as last_updated, "
                "AVG(price_usd) as avg_price_usd "
                "FROM buylists WHERE vendor = ?",
                (VENDOR,),
            ).fetchone()
            return {
                "source": self.name,
                "row_count": row["cnt"] if row else 0,
                "last_updated": row["last_updated"] if row else None,
                "avg_price_usd": (
                    round(row["avg_price_usd"], 4)
                    if row and row["avg_price_usd"] is not None else None
                ),
                **(dict(sync) if sync else {}),
            }
        finally:
            conn.close()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _fetch_all_rows(
        self, emit: Optional[EmitFn], warnings: list[str]
    ) -> list[dict]:
        """Fetch all buylist rows from CK.

        Tries the JSON API first (structured, fast). Falls back to HTML
        scraping of the landing page if the JSON API is unavailable.
        Returns a list of {"name", "set_code", "price"} dicts.
        """
        rows = self._fetch_json_api(emit, warnings)
        if rows:
            return rows
        # JSON API returned nothing — fall back to HTML scrape of landing page
        logger.info("[buylist_ck] JSON API returned no data; "
                    "falling back to HTML scrape")
        warnings.append(
            "JSON API returned no results; using HTML fallback scrape.")
        return self._fetch_html_page(warnings)

    def _fetch_json_api(
        self, emit: Optional[EmitFn], warnings: list[str]
    ) -> list[dict]:
        """Hit the CK JSON API paginated endpoint."""
        rows: list[dict] = []
        page = 1
        last_page = 1

        with httpx.Client(
            timeout=TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            while page <= last_page:
                if page > 1:
                    time.sleep(RATE_LIMIT_S)

                try:
                    resp = client.get(
                        CK_API_URL,
                        params={
                            "filter[buylist]": "1",
                            "page": page,
                        },
                    )
                except Exception as exc:
                    logger.debug("[buylist_ck] JSON API page %d error: %s",
                                 page, exc)
                    break

                if resp.status_code == 404 or resp.status_code == 422:
                    # API endpoint not found or bad params — give up
                    break

                if resp.status_code == 429:
                    time.sleep(2.0)
                    continue

                if not resp.is_success:
                    logger.debug("[buylist_ck] JSON API HTTP %d on page %d",
                                 resp.status_code, page)
                    break

                try:
                    data = resp.json()
                except Exception:
                    break

                # Shape check: expect {"data": [...], "meta": {...}}
                if not isinstance(data, dict) or "data" not in data:
                    break

                meta = data.get("meta") or {}
                last_page = int(meta.get("last_page") or 1)

                for item in (data.get("data") or []):
                    name = item.get("name") or item.get("product_name") or ""
                    set_code = (item.get("setCode") or item.get("set_code")
                                or item.get("edition_code") or "")
                    # CK buylist uses "buyPrice" or "buy_price"
                    price_raw = (item.get("buyPrice") or item.get("buy_price")
                                 or item.get("price") or "")
                    if name:
                        rows.append({
                            "name": name,
                            "set_code": set_code,
                            "price": price_raw,
                        })

                if page % 10 == 0 or page == last_page:
                    _emit_progress(
                        emit, page, last_page,
                        f"JSON API: page {page}/{last_page} "
                        f"({len(rows)} rows so far) …",
                        self.name,
                    )
                page += 1

        return rows

    def _fetch_html_page(self, warnings: list[str]) -> list[dict]:
        """Scrape the CK buylist HTML landing page for card rows.

        This is the fallback path when the JSON API is unavailable. The
        landing page lists cards in a table with .productRow elements
        containing the card name, set name, and price.
        """
        rows: list[dict] = []
        try:
            resp = httpx.get(
                CK_BUYLIST_PAGE_URL,
                timeout=TIMEOUT_S,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                },
                follow_redirects=True,
            )
            resp.raise_for_status()
        except Exception as exc:
            warnings.append(f"HTML fallback fetch failed: {exc}")
            return rows

        soup = BeautifulSoup(resp.text, "html.parser")
        rows = _parse_html_rows(soup)
        return rows

    @staticmethod
    def _write(conn, buylist_rows: list[dict]) -> tuple[int, int]:
        """Atomically replace all vendor='ck' rows.

        Returns (rows_changed, previous_row_count).
        """
        prev_count = conn.execute(
            "SELECT COUNT(*) FROM buylists WHERE vendor = ?",
            (VENDOR,),
        ).fetchone()[0]

        with conn:
            conn.execute(
                "DELETE FROM buylists WHERE vendor = ?",
                (VENDOR,),
            )
            conn.executemany(
                """INSERT INTO buylists
                       (oracle_id, vendor, price_usd, last_updated)
                   VALUES (:oracle_id, :vendor, :price_usd, :last_updated)""",
                buylist_rows,
            )

        return len(buylist_rows), prev_count


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _parse_html_rows(soup: BeautifulSoup) -> list[dict]:
    """Parse .productRow elements from CK buylist HTML.

    Expected structure (see tests/fixtures/buylist_ck/buylist_page.html):
      <tr class="productRow">
        <td class="productDesc">
          <span class="productDetailTitle">CARD NAME</span>
          <em class="edition">SET NAME</em>
        </td>
        <td class="addToCart">
          <input name="iSetCode" value="SET_CODE" />
          <span class="stylePrice">$X.XX</span>
        </td>
      </tr>
    """
    rows: list[dict] = []
    for tr in soup.select("tr.productRow"):
        name_el = tr.select_one(".productDetailTitle")
        price_el = tr.select_one(".stylePrice")
        set_input = tr.select_one("input[name='iSetCode']")

        if not name_el or not price_el:
            continue

        name = name_el.get_text(strip=True)
        price_raw = price_el.get_text(strip=True)
        set_code = (set_input.get("value") or "") if set_input else ""

        if name:
            rows.append({
                "name": name,
                "set_code": set_code.lower(),
                "price": price_raw,
            })
    return rows


def _parse_price(raw) -> Optional[float]:
    """Parse a price like '$1.50', '1.50', or 1.5 to float.

    Returns None if unparseable or negative.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        v = float(raw)
        return v if v >= 0 else None
    s = str(raw).strip().lstrip("$").strip()
    if not s:
        return None
    try:
        v = float(s)
        return v if v >= 0 else None
    except (ValueError, TypeError):
        return None


def _build_name_index() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """Build two lookup dicts from cards.CARD_DATA_BY_ID:

    name_index:      {name_lower: oracle_id}  — only for names that are unique
    name_set_index:  {(name_lower, set_lower): oracle_id}
    """
    try:
        import cards as _cards
        card_data_by_id = _cards.CARD_DATA_BY_ID
    except (ImportError, AttributeError):
        return {}, {}

    name_set_index: dict[tuple[str, str], str] = {}
    name_counts: dict[str, int] = {}
    name_oracle: dict[str, str] = {}

    for card in card_data_by_id.values():
        oracle_id = card.get("oracle_id")
        name = card.get("name")
        set_code = (card.get("set") or "").lower()
        if not oracle_id or not name:
            continue

        name_l = name.lower()
        name_set_index[(name_l, set_code)] = oracle_id

        # Track if name uniquely maps to one oracle_id
        if name_l not in name_counts:
            name_counts[name_l] = 0
            name_oracle[name_l] = oracle_id
        name_counts[name_l] += 1
        if name_oracle[name_l] != oracle_id:
            name_oracle[name_l] = None  # type: ignore[assignment]

    # Only keep unique-name mappings
    name_index = {
        n: oid
        for n, oid in name_oracle.items()
        if oid is not None and name_counts.get(n, 0) > 0
    }
    return name_index, name_set_index


def _resolve_oracle_id(
    name: str,
    set_code: str,
    name_index: dict[str, str],
    name_set_index: dict[tuple[str, str], str],
) -> Optional[str]:
    """Resolve a (name, set_code) pair to oracle_id.

    Priority:
      1. (name_lower, set_lower) exact match
      2. name_lower unique-name fallback
    """
    name_l = name.lower()
    set_l = set_code.lower() if set_code else ""

    # Priority 1: set-specific match
    if set_l:
        oid = name_set_index.get((name_l, set_l))
        if oid:
            return oid

    # Priority 2: unique-name fallback
    return name_index.get(name_l)


def _compute_coverage_pct(row_count: int) -> float:
    """Coverage pct: ratio of resolved rows vs total Oracle card universe.

    Approximate — CK only lists cards they're buying, not all ~30k oracle
    cards, so we treat the row count itself as the metric. Returns 100.0
    if row_count > 0 (meaning: the fetch succeeded and we have data).
    """
    return 100.0 if row_count > 0 else 0.0


def _record_sync(source: str, success: bool,
                 error: Optional[str] = None) -> None:
    conn = enrichment_db.get_connection()
    try:
        enrichment_db.record_sync_attempt(
            conn, source, success=success, error=error)
    finally:
        conn.close()


def _emit_progress(
    emit: Optional[EmitFn],
    step: int, total: int,
    message: str,
    source: str,
) -> None:
    if emit is None:
        return
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        emit("enrichment_refresh_progress", {
            "source": source,
            "step": step, "total": total,
            "message": message, "ts": ts,
        })
    except Exception:
        pass


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)
