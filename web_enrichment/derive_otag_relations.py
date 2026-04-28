# web_enrichment/derive_otag_relations.py
# ---------------------------------------------------------------------------
# Derivation pipeline for otag relationship graph.
#
# Reads the populated `tags` table (oracle_id → otag mappings) and writes
# typed relationship edges into the `otag_relations` table.
#
# NOT an EnrichmentSource — this is a post-processing step that runs after
# TaggerSource.refresh() populates the tags table.  Call derive_all(conn)
# from a TaggerSource post-refresh hook, or run as a CLI script:
#
#   python -m web_enrichment.derive_otag_relations
#
# Relation types written:
#   implies          — every card with src also has dst, AND |dst| > |src|
#   hierarchy        — implies edge where |dst| > 1.5 × |src|, after
#                      transitive reduction
#   synonym          — Jaccard ≥ 0.95 (bidirectional)
#   co_occurs        — 0.05 ≤ Jaccard < 0.95 (bidirectional); weight=Jaccard
#   sibling_disjoint — share a parent in hierarchy AND Jaccard < 0.01
#   related          — 0.01 ≤ Jaccard < 0.05 (bidirectional); weight=Jaccard
#
# Performance: ~1500 otags → ~1.1M pair candidates. Builds a
# Dict[otag, frozenset[oracle_id]] in memory (~30MB), then iterates pairs
# once via a sorted-tag-list approach, dispatching each into the right bucket.
# Expected runtime: 1-3 minutes for the full catalogue.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# Relation type constants
REL_IMPLIES = "implies"
REL_HIERARCHY = "hierarchy"
REL_SYNONYM = "synonym"
REL_CO_OCCURS = "co_occurs"
REL_SIBLING_DISJOINT = "sibling_disjoint"
REL_RELATED = "related"

# Thresholds
SYNONYM_JACCARD = 0.95
CO_OCCURS_MIN_JACCARD = 0.05
RELATED_MIN_JACCARD = 0.01
HIERARCHY_RATIO = 1.5  # |dst| must be > HIERARCHY_RATIO * |src|


def _now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_tag_sets(conn: sqlite3.Connection) -> dict[str, frozenset]:
    """Load all otag → frozenset(oracle_ids) from the tags table.

    Only loads tags that appear in tag_catalog with tag_type='function'.
    This avoids pulling in legacy art-tag data that may have been stored
    with an 'atag:' prefix in the tags table.
    """
    # Load the set of known function tags from catalog
    catalog_rows = conn.execute(
        "SELECT tag_name FROM tag_catalog WHERE tag_type='function'"
    ).fetchall()
    if catalog_rows:
        known_function_tags: Optional[set] = {
            r[0] if isinstance(r, tuple) else r["tag_name"]
            for r in catalog_rows
        }
    else:
        known_function_tags = None  # no catalog — load all tags

    if known_function_tags is not None:
        rows = conn.execute(
            "SELECT oracle_id, tag_name FROM tags "
            "WHERE tag_name IN ({})".format(
                ",".join("?" * len(known_function_tags))
            ),
            list(known_function_tags),
        ).fetchall() if known_function_tags else []
    else:
        rows = conn.execute(
            "SELECT oracle_id, tag_name FROM tags"
        ).fetchall()

    tag_sets: dict[str, set] = {}
    for row in rows:
        if isinstance(row, tuple):
            oid, tag = row
        else:
            oid, tag = row["oracle_id"], row["tag_name"]
        tag_sets.setdefault(tag, set()).add(oid)

    return {tag: frozenset(cards) for tag, cards in tag_sets.items()}


def _jaccard(a: frozenset, b: frozenset) -> float:
    """Jaccard similarity of two sets. Returns 0.0 if both are empty."""
    union_size = len(a | b)
    if union_size == 0:
        return 0.0
    return len(a & b) / union_size


def _is_subset(small: frozenset, large: frozenset) -> bool:
    """Return True if every element of small is in large."""
    return small <= large


def _transitive_reduction(
    edges: set[tuple[str, str]]
) -> set[tuple[str, str]]:
    """Remove edges (X, Z) from a DAG where a path X → Y → Z already exists.

    Operates on the set of direct implies/hierarchy edges.  Only removes
    transitive shortcuts — the result is the minimal set of edges that
    preserves reachability.

    This is an approximation (not perfectly efficient), but it's fast
    enough for ~5k hierarchy edges.
    """
    # Build adjacency: parent → set of direct children
    children: dict[str, set] = {}
    for src, dst in edges:
        children.setdefault(src, set()).add(dst)

    def reachable_via_intermediates(src: str, dst: str) -> bool:
        """True if dst is reachable from src via some intermediate node."""
        direct_children = children.get(src, set())
        for mid in direct_children:
            if mid == dst:
                continue  # skip the direct edge itself
            if dst in _all_descendants(mid, children):
                return True
        return False

    result: set[tuple[str, str]] = set()
    for src, dst in edges:
        if not reachable_via_intermediates(src, dst):
            result.add((src, dst))
    return result


def _all_descendants(node: str, children: dict[str, set],
                     _visited: Optional[set] = None) -> set:
    """DFS to collect all descendants of node."""
    if _visited is None:
        _visited = set()
    if node in _visited:
        return _visited
    _visited.add(node)
    for child in children.get(node, set()):
        _all_descendants(child, children, _visited)
    return _visited


def derive_all(conn: sqlite3.Connection) -> dict[str, int]:
    """Derive all otag relationships and write them to otag_relations.

    Clears source='derived' rows before re-inserting (full-rebuild semantics).
    Manual rows (source='manual') are never touched.

    Args:
        conn: open enrichment.db connection.

    Returns:
        Dict mapping relation_type -> count of edges written.
    """
    ts = _now_ts()
    logger.info("derive_otag_relations: loading tag sets …")
    tag_sets = load_tag_sets(conn)
    n = len(tag_sets)
    logger.info("derive_otag_relations: %d tags loaded; computing pairs …", n)

    tags = sorted(tag_sets.keys())
    counts: dict[str, int] = {
        REL_IMPLIES: 0,
        REL_HIERARCHY: 0,
        REL_SYNONYM: 0,
        REL_CO_OCCURS: 0,
        REL_SIBLING_DISJOINT: 0,
        REL_RELATED: 0,
    }

    # --- Pass 1: compute implies + synonym + co_occurs + related ----------
    # Accumulate (src, dst, relation_type, weight) rows.
    # Implies edges are also collected for hierarchy derivation.
    implies_edges: set[tuple[str, str]] = set()  # (child, parent)

    rows: list[tuple[str, str, str, float, str, str]] = []
    # (src_otag, dst_otag, relation_type, weight, source, last_updated)

    for i, tag_a in enumerate(tags):
        set_a = tag_sets[tag_a]
        if not set_a:
            continue
        for tag_b in tags[i + 1:]:
            set_b = tag_sets[tag_b]
            if not set_b:
                continue

            inter = len(set_a & set_b)
            if inter == 0:
                continue  # no overlap at all — no relation

            union = len(set_a | set_b)
            j = inter / union

            if j >= SYNONYM_JACCARD:
                # Synonym — bidirectional
                rows.append((tag_a, tag_b, REL_SYNONYM, j, "derived", ts))
                rows.append((tag_b, tag_a, REL_SYNONYM, j, "derived", ts))

            elif j >= CO_OCCURS_MIN_JACCARD:
                # co_occurs — bidirectional
                rows.append((tag_a, tag_b, REL_CO_OCCURS, j, "derived", ts))
                rows.append((tag_b, tag_a, REL_CO_OCCURS, j, "derived", ts))

                # Check for implies in either direction
                len_a, len_b = len(set_a), len(set_b)
                if inter == len_a and len_a < len_b:
                    # Every tag_a card also has tag_b AND tag_b is larger
                    implies_edges.add((tag_a, tag_b))
                elif inter == len_b and len_b < len_a:
                    implies_edges.add((tag_b, tag_a))

            elif j >= RELATED_MIN_JACCARD:
                # related — bidirectional, low weight
                rows.append((tag_a, tag_b, REL_RELATED, j, "derived", ts))
                rows.append((tag_b, tag_a, REL_RELATED, j, "derived", ts))

                # Implies can still hold at low Jaccard if one set is tiny
                len_a, len_b = len(set_a), len(set_b)
                if inter == len_a and len_a < len_b:
                    implies_edges.add((tag_a, tag_b))
                elif inter == len_b and len_b < len_a:
                    implies_edges.add((tag_b, tag_a))

            else:
                # Jaccard 0 < j < 0.01 — check implies only
                len_a, len_b = len(set_a), len(set_b)
                if inter == len_a and len_a < len_b:
                    implies_edges.add((tag_a, tag_b))
                elif inter == len_b and len_b < len_a:
                    implies_edges.add((tag_b, tag_a))

    # Also catch pure subset-implies that weren't caught above
    # (very small sets — e.g. 1-card tag fully inside large tag)
    for i, tag_a in enumerate(tags):
        set_a = tag_sets[tag_a]
        if not set_a:
            continue
        for tag_b in tags:
            if tag_a == tag_b:
                continue
            set_b = tag_sets[tag_b]
            if not set_b:
                continue
            if set_a < set_b:  # proper subset
                implies_edges.add((tag_a, tag_b))

    # Write implies rows (one direction only — src ⊂ dst)
    for src, dst in implies_edges:
        rows.append((src, dst, REL_IMPLIES, 1.0, "derived", ts))

    # --- Pass 2: hierarchy from implies ----------------------------------------
    # A hierarchy edge is an implies edge where |dst| > HIERARCHY_RATIO * |src|
    hierarchy_candidates: set[tuple[str, str]] = set()
    for src, dst in implies_edges:
        len_src = len(tag_sets[src])
        len_dst = len(tag_sets[dst])
        if len_dst > HIERARCHY_RATIO * len_src:
            hierarchy_candidates.add((src, dst))

    # Transitive reduction to keep only direct parent-child edges
    hierarchy_edges = _transitive_reduction(hierarchy_candidates)

    for src, dst in hierarchy_edges:
        rows.append((src, dst, REL_HIERARCHY, 1.0, "derived", ts))

    # Populate tag_catalog.parent: for each child, pick the parent
    # with the smallest card count (most specific immediate parent).
    child_parents: dict[str, list[str]] = {}
    for src, dst in hierarchy_edges:
        child_parents.setdefault(src, []).append(dst)

    parent_updates: list[tuple[str, str]] = []  # (parent, tag_name)
    for child, parents in child_parents.items():
        best = min(parents, key=lambda p: len(tag_sets.get(p, set())))
        parent_updates.append((best, child))

    # --- Pass 3: sibling_disjoint -----------------------------------------------
    # Pairs of tags that share a hierarchy parent AND Jaccard < 0.01
    siblings_by_parent: dict[str, list[str]] = {}
    for src, dst in hierarchy_edges:
        siblings_by_parent.setdefault(dst, []).append(src)

    for parent, sibs in siblings_by_parent.items():
        for i2, sib_a in enumerate(sibs):
            set_a = tag_sets.get(sib_a, frozenset())
            for sib_b in sibs[i2 + 1:]:
                set_b = tag_sets.get(sib_b, frozenset())
                j = _jaccard(set_a, set_b)
                if j < 0.01:
                    rows.append(
                        (sib_a, sib_b, REL_SIBLING_DISJOINT, 1.0,
                         "derived", ts))
                    rows.append(
                        (sib_b, sib_a, REL_SIBLING_DISJOINT, 1.0,
                         "derived", ts))

    # --- Write to DB ------------------------------------------------------------
    logger.info(
        "derive_otag_relations: writing %d relation rows + %d hierarchy edges …",
        len(rows), len(hierarchy_edges)
    )

    with conn:
        # Clear all derived rows
        conn.execute("DELETE FROM otag_relations WHERE source='derived'")

        # Insert new derived rows
        conn.executemany(
            """INSERT INTO otag_relations
                   (src_otag, dst_otag, relation_type, weight, source, last_updated)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(src_otag, dst_otag, relation_type) DO UPDATE SET
                   weight       = excluded.weight,
                   last_updated = excluded.last_updated""",
            rows,
        )

        # Update tag_catalog.parent (only for derived hierarchy)
        if parent_updates:
            conn.executemany(
                "UPDATE tag_catalog SET parent = ? WHERE tag_name = ?",
                parent_updates,
            )

    # Count actual rows in DB
    for rel_type in list(counts.keys()):
        count = conn.execute(
            "SELECT COUNT(*) FROM otag_relations "
            "WHERE relation_type=? AND source='derived'",
            (rel_type,)
        ).fetchone()[0]
        counts[rel_type] = count

    logger.info("derive_otag_relations: done — %s", counts)
    return counts


def main() -> None:
    """CLI entry point: run derivation against the live enrichment.db."""
    import enrichment_db

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    t0 = time.time()
    conn = enrichment_db.get_connection()
    try:
        counts = derive_all(conn)
    finally:
        conn.close()
    elapsed = time.time() - t0
    print(f"Done in {elapsed:.1f}s")
    for rel, count in sorted(counts.items()):
        print(f"  {rel:20s}: {count:>8,}")


if __name__ == "__main__":
    main()
