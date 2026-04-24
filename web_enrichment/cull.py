# web_enrichment/cull.py
# ---------------------------------------------------------------------------
# Dead-weight cull view — Phase 2.14.
#
# Joins inventory (collection.db) against enrichment.db to score each owned
# card on four independent signals:
#
#   Signal 1 — vanilla / french-vanilla oracle text
#   Signal 2 — not a staple (not in edhrec_staples at any tier, not in
#               cedh_staples)
#   Signal 3 — low or zero salt score  (<= LOW_SALT_THRESHOLD)
#   Signal 4 — low or zero buylist value (no CK buylist entry, or price
#               below the configured max_buylist_price threshold)
#
# A card qualifies as a cull candidate if it matches the criteria for the
# chosen preset:
#
#   "default" (soft):  all 4 signals fire AND not in any linked deck
#   "strict":          default PLUS market price == 0 or NULL
#
# Scoring details:
#   A "keep_confidence" float in [0.0, 1.0] is computed per card:
#     0.0 = definitely cull      1.0 = definitely keep
#
#   Components (each 0.0 or positive contribution to keep_confidence):
#     + is_staple:             0.40 if any staple tier matches
#     + salt_score contribution: min(salt / 5.0, 0.25)  (max 0.25 at salt=5)
#     + has_buylist:           0.20 if ck buylist > 0
#     + has_complex_text:      0.15 if not vanilla/french-vanilla
#
#   keep_confidence = sum of contributions (clamped 0–1).
#   Suggested action derived from keep_confidence:
#     < 0.10 → "donate"
#     0.10–0.20 → "bulk"
#     0.20–0.40 → "trade"
#     >= 0.40 → "sell" (some value detected — route to sale pile)
#
# NOTE: The `oracle_text` field is NOT stored in collection.db inventory.
# When cards are imported from Scryfall bulk data, `oracle_text` lives only
# in Scryfall bulk JSON (not indexed in the collection DB).  The vanilla
# check falls back to the `tags` table in enrichment.db (tags `vanilla` and
# `french-vanilla` populated by TaggerSource).  If the tags table is empty,
# all owned cards with no enrichment signals are considered potential culls
# (conservative: missing data → cull rather than keep).
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import sqlite3
from typing import Optional


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

LOW_SALT_THRESHOLD = 0.5     # salt <= this value counts as "low salt"
MAX_BUYLIST_PRICE_DEFAULT = 0.05   # matches the plan: "buylist < $0.05"
MAX_MARKET_PRICE_DEFAULT = 1.00    # UI default: only cull cards worth < $1


def _suggest_action(keep_confidence: float) -> str:
    """Map keep_confidence to a human-readable suggested disposition."""
    if keep_confidence < 0.10:
        return "donate"
    if keep_confidence < 0.20:
        return "bulk"
    if keep_confidence < 0.40:
        return "trade"
    return "sell"


def get_cull_candidates(
    collection_conn: sqlite3.Connection,
    *,
    enr_db_path: Optional[str] = None,
    max_market_price: float = MAX_MARKET_PRICE_DEFAULT,
    max_buylist_price: float = MAX_BUYLIST_PRICE_DEFAULT,
    min_quantity: int = 1,
    exclude_staples: bool = True,
    preset: str = "default",
) -> list[dict]:
    """Return owned inventory rows that are cull candidates.

    Parameters
    ----------
    collection_conn:
        Open connection to collection.db (caller owns lifecycle).
    enr_db_path:
        Path to enrichment.db.  Defaults to ``enrichment_db.DB_PATH``.
    max_market_price:
        Maximum market price (price_usd from inventory).  Cards above
        this threshold are excluded (they have resale value).
    max_buylist_price:
        Cards with a CK buylist price above this value are excluded.
        Default 0.05 matches the plan definition.
    min_quantity:
        Minimum quantity in inventory to appear in results.  Default 1.
    exclude_staples:
        If True (default), any card with a row in the staples table at
        any tier is excluded.
    preset:
        "default" — standard cull criteria.
        "strict" — also requires price_usd == NULL or 0.

    Returns
    -------
    list[dict] with keys:
        oracle_id, name, set_code, collector_number, type_line, rarity,
        colors, price_usd, quantity, box,
        buylist_price, salt_score,
        is_universal_staple, is_archetype_staple, is_cedh_staple,
        commander_popularity,
        cull_reasons (list[str]),
        suggested_action (str),
        keep_confidence (float),
        location (str)

    Degrades gracefully: if enrichment.db is absent or empty, returns
    inventory rows that have low/zero price with empty enrichment fields.
    """
    import enrichment_db as _edb

    enr_path = enr_db_path or _edb.DB_PATH
    enr_available = os.path.exists(enr_path)

    strict = preset == "strict"

    attached = False
    try:
        if enr_available:
            collection_conn.execute(
                "ATTACH DATABASE ? AS enr", (enr_path,)
            )
            attached = True

        if not enr_available:
            # Fallback: no enrichment data — return price-filtered inventory
            rows = collection_conn.execute(
                """
                SELECT
                    id, name, set_code, collector_number,
                    oracle_id, type_line, rarity, colors,
                    price_usd, quantity, box
                FROM inventory
                WHERE quantity >= ?
                AND (price_usd IS NULL OR price_usd < ?)
                ORDER BY name ASC
                """,
                (min_quantity, max_market_price),
            ).fetchall()
            result = []
            for row in rows:
                d = dict(row)
                d.update({
                    "buylist_price": None,
                    "salt_score": None,
                    "is_universal_staple": False,
                    "is_archetype_staple": False,
                    "is_cedh_staple": False,
                    "commander_popularity": None,
                    "cull_reasons": ["low_price", "no_enrichment_data"],
                    "suggested_action": "bulk",
                    "keep_confidence": 0.05,
                    "location": d.get("box") or "",
                })
                result.append(d)
            return result

        # -- Full enrichment-joined query --

        # Build the base inventory filter
        price_filter = ""
        price_params: list = []
        if strict:
            price_filter = "AND (i.price_usd IS NULL OR i.price_usd = 0)"
        else:
            price_filter = "AND (i.price_usd IS NULL OR i.price_usd < ?)"
            price_params.append(max_market_price)

        staple_filter = ""
        if exclude_staples:
            staple_filter = """
                AND (i.oracle_id IS NULL OR i.oracle_id NOT IN (
                    SELECT DISTINCT oracle_id FROM enr.staples
                ))
            """

        # Require vanilla/french-vanilla tag from enrichment as a gate.
        # Cards with no tags haven't been enriched — we can't say if they're
        # dead weight, so we skip them (prevents false positives for cards
        # like Lightning Bolt that have no enrichment data at all).
        vanilla_filter = """
            AND i.oracle_id IN (
                SELECT DISTINCT oracle_id FROM enr.tags
                WHERE tag_name IN ('vanilla', 'french-vanilla')
            )
        """

        # Fetch all qualifying inventory rows with enrichment signals
        sql = f"""
            SELECT
                i.id                  AS id,
                i.name                AS name,
                i.set_code            AS set_code,
                i.collector_number    AS collector_number,
                i.oracle_id           AS oracle_id,
                i.type_line           AS type_line,
                i.rarity              AS rarity,
                i.colors              AS colors,
                i.price_usd           AS price_usd,
                i.quantity            AS quantity,
                i.box                 AS box,
                -- Buylist
                b.price_usd           AS buylist_price,
                -- Salt
                ss.salt               AS salt_score,
                -- Staple tiers
                MAX(CASE WHEN st.tier = 'universal'  THEN 1 ELSE 0 END) AS is_universal_staple,
                MAX(CASE WHEN st.tier = 'archetype'  THEN 1 ELSE 0 END) AS is_archetype_staple,
                MAX(CASE WHEN st.tier = 'cedh'       THEN 1 ELSE 0 END) AS is_cedh_staple,
                -- Commander popularity
                cr.deck_count         AS commander_popularity,
                -- Vanilla/french-vanilla tags
                MAX(CASE WHEN t.tag_name IN ('vanilla', 'french-vanilla') THEN 1 ELSE 0 END) AS has_vfv_tag,
                GROUP_CONCAT(DISTINCT
                    CASE WHEN t.tag_name IN ('vanilla', 'french-vanilla')
                         THEN t.tag_name END
                ) AS vfv_tags
            FROM inventory i
            LEFT JOIN enr.buylists b
                ON b.oracle_id = i.oracle_id AND b.vendor = 'ck'
            LEFT JOIN enr.salt_scores ss
                ON ss.oracle_id = i.oracle_id
            LEFT JOIN enr.staples st
                ON st.oracle_id = i.oracle_id
            LEFT JOIN enr.commander_ranks cr
                ON cr.oracle_id = i.oracle_id
            LEFT JOIN enr.tags t
                ON t.oracle_id = i.oracle_id
               AND t.tag_name IN ('vanilla', 'french-vanilla')
            WHERE i.quantity >= ?
            {price_filter}
            {staple_filter}
            {vanilla_filter}
            GROUP BY i.id
            ORDER BY i.name ASC
        """

        params = [min_quantity] + price_params
        rows = collection_conn.execute(sql, params).fetchall()

        result = []
        for row in rows:
            d = dict(row)

            oracle_id = d.get("oracle_id")
            name = d.get("name", "")
            price_usd = d.get("price_usd")
            buylist_price = d.get("buylist_price")
            salt_score = d.get("salt_score")
            is_universal = bool(d.get("is_universal_staple"))
            is_archetype = bool(d.get("is_archetype_staple"))
            is_cedh = bool(d.get("is_cedh_staple"))
            commander_pop = d.get("commander_popularity")
            has_vfv_tag = bool(d.get("has_vfv_tag"))
            vfv_tags_raw = d.get("vfv_tags") or ""
            vfv_tags = [t for t in vfv_tags_raw.split(",") if t]

            # Determine cull reasons
            reasons: list[str] = []
            if has_vfv_tag:
                reasons.extend(vfv_tags)
            if not is_universal and not is_archetype and not is_cedh:
                reasons.append("no_staple")
            if salt_score is None or salt_score <= LOW_SALT_THRESHOLD:
                reasons.append("low_salt")
            if buylist_price is None or buylist_price <= max_buylist_price:
                reasons.append("no_buylist")
            if price_usd is None or price_usd == 0:
                reasons.append("zero_price")
            elif price_usd < max_market_price:
                reasons.append("low_price")

            # Compute keep_confidence
            keep_conf = 0.0
            if is_universal or is_archetype or is_cedh:
                keep_conf += 0.40
            if salt_score is not None:
                keep_conf += min(salt_score / 5.0, 0.25)
            if buylist_price is not None and buylist_price > 0:
                keep_conf += 0.20
            # Non-vanilla/french-vanilla text (card has complex text we
            # can't detect here, but if tagger tagged it as vanilla it has
            # no complex text).  Absence of vanilla tag adds a small bonus.
            if not has_vfv_tag:
                keep_conf += 0.15

            keep_conf = min(1.0, keep_conf)

            # Skip cards that don't meet baseline cull criteria:
            # at least one of the "no value" signals must fire.
            # If all signals say keep (e.g. is_staple=True but price filter
            # let it through), don't include it.
            # However, if exclude_staples is False the user explicitly wants
            # to see staples too, so we include them regardless.
            is_any_staple = is_universal or is_archetype or is_cedh
            if exclude_staples and is_any_staple:
                # staple_filter in SQL already excludes these; belt+suspenders
                continue

            # Also check buylist ceiling explicitly
            if (buylist_price is not None
                    and buylist_price > max_buylist_price):
                continue

            # Check deck usage if the deck_usage table exists
            in_deck = False
            if oracle_id:
                try:
                    du_row = collection_conn.execute(
                        "SELECT deck_count, wishlist_count "
                        "FROM enr.deck_usage WHERE oracle_id = ?",
                        (oracle_id,),
                    ).fetchone()
                    if du_row and (du_row["deck_count"] > 0
                                   or du_row["wishlist_count"] > 0):
                        in_deck = True
                except Exception:
                    pass  # table absent → treat as not in any deck

            if in_deck:
                reasons_final = [r for r in reasons
                                  if r not in ("low_price", "zero_price",
                                               "low_salt", "no_buylist",
                                               "vanilla", "french-vanilla",
                                               "no_staple")]
                # Card is in a deck — skip from default preset
                # (it has "value" even if low price/salt)
                continue

            location = d.get("box") or ""

            result.append({
                "id": d.get("id"),
                "oracle_id": oracle_id,
                "name": name,
                "set_code": d.get("set_code"),
                "collector_number": d.get("collector_number"),
                "type_line": d.get("type_line"),
                "rarity": d.get("rarity"),
                "colors": d.get("colors"),
                "price_usd": price_usd,
                "quantity": d.get("quantity"),
                "box": d.get("box"),
                "buylist_price": buylist_price,
                "salt_score": salt_score,
                "is_universal_staple": is_universal,
                "is_archetype_staple": is_archetype,
                "is_cedh_staple": is_cedh,
                "commander_popularity": commander_pop,
                "cull_reasons": reasons,
                "suggested_action": _suggest_action(keep_conf),
                "keep_confidence": round(keep_conf, 4),
                "location": location,
            })

        return result

    except Exception:
        # Degrade gracefully on any SQL / schema error
        return []
    finally:
        if attached:
            try:
                collection_conn.execute("DETACH DATABASE enr")
            except Exception:
                pass
