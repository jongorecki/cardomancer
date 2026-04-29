# web_enrichment/compute_otag_clusters.py
# ---------------------------------------------------------------------------
# Louvain community detection over the otag co_occurs graph.
#
# Reads otag_relations rows where relation_type='co_occurs', builds an
# undirected weighted graph, runs networkx's Louvain community detection,
# and writes the assigned community id back to tag_catalog.cluster_id for
# every otag.
#
# Powers the Atlas mode of the Otag Explorer (templates/index.html). The
# computation is one-shot; cluster ids persist until the next run. Re-run
# whenever tag membership changes meaningfully (after a TaggerSource
# refresh + derive_otag_relations cycle).
#
# CLI:
#   python -m web_enrichment.compute_otag_clusters
#
# Skip cleanly if networkx isn't installed — the caller (the /api/otags
# endpoint) prints a one-line install hint and falls back to connected
# components.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import sqlite3
import sys
from typing import Optional

logger = logging.getLogger(__name__)


def networkx_available() -> bool:
    """True if networkx is importable. Cheap and idempotent."""
    try:
        import networkx  # noqa: F401
        return True
    except ImportError:
        return False


def compute_all(conn: sqlite3.Connection,
                seed: int = 42) -> tuple[int, int]:
    """Compute Louvain communities and write cluster_id back.

    Returns:
        (num_clusters, num_tags_assigned)

    Algorithm:
      1. Read all (src, dst, weight) rows from otag_relations where
         relation_type='co_occurs'.
      2. Build an undirected weighted networkx Graph. Co-occurs edges are
         already symmetric in the table (both (a,b) and (b,a) rows exist
         for every Jaccard pair) — networkx dedupes when adding.
      3. Run nx.community.louvain_communities(G, weight='weight', seed=seed).
      4. Sort communities by size descending, assign id=0,1,2,... so the
         largest cluster is id 0.
      5. UPSERT cluster_id back into tag_catalog (only for function-type tags).

    Raises ImportError if networkx is not installed.
    """
    import networkx as nx  # late import; raises if missing

    rows = conn.execute(
        "SELECT src_otag, dst_otag, weight FROM otag_relations "
        "WHERE relation_type='co_occurs'"
    ).fetchall()
    if not rows:
        logger.warning("No co_occurs edges found; skipping cluster computation")
        return (0, 0)

    G = nx.Graph()
    for r in rows:
        src = r[0] if isinstance(r, tuple) else r["src_otag"]
        dst = r[1] if isinstance(r, tuple) else r["dst_otag"]
        wt = r[2] if isinstance(r, tuple) else r["weight"]
        if src == dst:
            continue
        if G.has_edge(src, dst):
            # Reverse edge already added; keep max weight (should be equal)
            existing = G[src][dst].get("weight", 1.0)
            G[src][dst]["weight"] = max(existing, float(wt or 0.0))
        else:
            G.add_edge(src, dst, weight=float(wt or 0.0))

    # Also add isolated nodes for every function tag so they get a cluster id
    # even if they have no co_occurs edges (singleton cluster).
    catalog = conn.execute(
        "SELECT tag_name FROM tag_catalog WHERE tag_type='function'"
    ).fetchall()
    for r in catalog:
        name = r[0] if isinstance(r, tuple) else r["tag_name"]
        if not G.has_node(name):
            G.add_node(name)

    logger.info("Co-occurs graph: %d nodes, %d edges", G.number_of_nodes(),
                G.number_of_edges())

    communities = nx.community.louvain_communities(
        G, weight="weight", seed=seed
    )
    # Sort by size descending so the most populous cluster is id 0
    communities.sort(key=len, reverse=True)

    tag_to_cluster: dict[str, int] = {}
    for cid, members in enumerate(communities):
        for tag in members:
            tag_to_cluster[tag] = cid

    # UPSERT cluster_id back into tag_catalog
    with conn:
        # Reset previous cluster_ids first so removed tags don't keep stale ids
        conn.execute("UPDATE tag_catalog SET cluster_id = NULL "
                     "WHERE tag_type='function'")
        conn.executemany(
            "UPDATE tag_catalog SET cluster_id = ? WHERE tag_name = ?",
            [(cid, tag) for tag, cid in tag_to_cluster.items()]
        )

    logger.info("Wrote cluster_id for %d tags across %d clusters",
                len(tag_to_cluster), len(communities))
    return (len(communities), len(tag_to_cluster))


def compute_with_fallback(conn: sqlite3.Connection) -> tuple[int, int, str]:
    """Try Louvain via networkx; fall back to connected components.

    Returns (num_clusters, num_tags_assigned, mode) where mode is
    'louvain' or 'components'.
    """
    if networkx_available():
        n_clusters, n_tags = compute_all(conn)
        return (n_clusters, n_tags, "louvain")

    # Fallback: connected components on co_occurs graph (no weights).
    rows = conn.execute(
        "SELECT src_otag, dst_otag FROM otag_relations "
        "WHERE relation_type='co_occurs'"
    ).fetchall()
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for r in rows:
        a = r[0] if isinstance(r, tuple) else r["src_otag"]
        b = r[1] if isinstance(r, tuple) else r["dst_otag"]
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        union(a, b)

    # Add catalog tags as singletons
    catalog = conn.execute(
        "SELECT tag_name FROM tag_catalog WHERE tag_type='function'"
    ).fetchall()
    for r in catalog:
        name = r[0] if isinstance(r, tuple) else r["tag_name"]
        parent.setdefault(name, name)

    # Bucket by root and sort by size descending
    buckets: dict[str, list[str]] = {}
    for tag in parent:
        root = find(tag)
        buckets.setdefault(root, []).append(tag)
    sorted_groups = sorted(buckets.values(), key=len, reverse=True)

    tag_to_cluster: dict[str, int] = {}
    for cid, members in enumerate(sorted_groups):
        for tag in members:
            tag_to_cluster[tag] = cid

    with conn:
        conn.execute("UPDATE tag_catalog SET cluster_id = NULL "
                     "WHERE tag_type='function'")
        conn.executemany(
            "UPDATE tag_catalog SET cluster_id = ? WHERE tag_name = ?",
            [(cid, tag) for tag, cid in tag_to_cluster.items()]
        )

    return (len(sorted_groups), len(tag_to_cluster), "components")


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s"
    )

    if not networkx_available():
        print("networkx is not installed.")
        print("Install it with: pip install networkx")
        print("Falling back to connected-components grouping (lower quality).")

    import enrichment_db
    conn = enrichment_db.get_connection()
    try:
        n_clusters, n_tags, mode = compute_with_fallback(conn)
        print(f"Mode: {mode}")
        print(f"Clusters: {n_clusters}")
        print(f"Tags assigned: {n_tags}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
