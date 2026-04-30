# web_enrichment/otag_explorer.py
# ---------------------------------------------------------------------------
# Pure helpers powering the Otag Explorer tab (templates/index.html).
#
# Modes — Galaxy, Outline, Tree, Atlas — share five backend endpoints:
#   /api/otags/search          → autocomplete for cards or otags
#   /api/otags/neighborhood    → BFS subgraph around an otag (Galaxy / Outline)
#   /api/otags/by-card         → otags + hierarchy ancestors for a card (Tree)
#   /api/otags/clusters        → precomputed Louvain clusters (Atlas)
#   /api/otags/examples        → example cards for one or more otags (Outline)
#
# The web_server.py routes are thin wrappers around the helpers below so we
# can unit-test the logic without spinning up Flask. Each helper accepts an
# explicit sqlite3.Connection so tests can inject a tmp DB.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sqlite3
from collections import deque
from typing import Iterable, Optional


# Default relation types expanded by the Galaxy view. `related` and
# `sibling_disjoint` are intentionally excluded — they're huge (~146k +
# ~2.9M rows) and would saturate any neighborhood. The frontend exposes
# checkboxes to opt back in.
DEFAULT_NEIGHBORHOOD_TYPES = (
    "hierarchy",
    "synonym",
    "co_occurs",
    "implies",
)

VALID_RELATION_TYPES = (
    "hierarchy",
    "synonym",
    "co_occurs",
    "implies",
    "related",
    "sibling_disjoint",
)


# ---------------------------------------------------------------------------
# /api/otags/search
# ---------------------------------------------------------------------------

def search_cards_and_otags(
    cards_data: list,
    enrichment_conn: sqlite3.Connection,
    q: str,
    limit: int = 20,
) -> list[dict]:
    """Mixed autocomplete returning cards and otags.

    Cards: matched against name (lang='en' AND 'paper' in games). Prefix
    matches rank above substring matches. Within each rank tier, sort by
    edhrec_rank ascending (NULLs last).

    Otags: tag_catalog.tag_name where tag_type='function'. Substring match,
    ordered by card_count_expected desc (NULLs last).

    Total result is len(cards) + len(otags), bounded by `limit`. Each side
    receives at most limit//2 entries.
    """
    if not q:
        return []
    q_lower = q.strip().lower()
    if not q_lower:
        return []

    half = max(1, limit // 2)

    # ---- Cards ----------------------------------------------------------
    seen_names: set[str] = set()
    prefix_hits: list[tuple] = []
    substring_hits: list[tuple] = []
    for c in cards_data:
        if c.get("lang") != "en":
            continue
        if "paper" not in (c.get("games") or []):
            continue
        name = (c.get("name") or "")
        nm_l = name.lower()
        if not nm_l:
            continue
        if nm_l in seen_names:
            continue
        if nm_l.startswith(q_lower):
            seen_names.add(nm_l)
            prefix_hits.append((c.get("edhrec_rank"), c))
        elif q_lower in nm_l:
            seen_names.add(nm_l)
            substring_hits.append((c.get("edhrec_rank"), c))

    def _rank_key(t):
        rank = t[0]
        return (1, 0) if rank is None else (0, rank)

    prefix_hits.sort(key=_rank_key)
    substring_hits.sort(key=_rank_key)
    card_pool = (prefix_hits + substring_hits)[:half]

    card_results = []
    for _rank, c in card_pool:
        name = c.get("name") or ""
        slug = name.lower().replace("'", "").replace(",", "")
        slug = "-".join(slug.split())
        card_results.append({
            "kind": "card",
            "value": slug,
            "label": name,
            "set": c.get("set"),
            "cn": c.get("collector_number"),
            "scryfall_id": c.get("id"),
            "card_count": None,
        })

    # ---- Otags ----------------------------------------------------------
    pattern = f"%{q_lower}%"
    cur = enrichment_conn.execute(
        """
        SELECT tag_name, card_count_expected
          FROM tag_catalog
         WHERE tag_type = 'function'
           AND LOWER(tag_name) LIKE ?
         ORDER BY
           CASE WHEN card_count_expected IS NULL THEN 1 ELSE 0 END,
           card_count_expected DESC,
           tag_name ASC
         LIMIT ?
        """,
        (pattern, half),
    )
    otag_results = [
        {
            "kind": "otag",
            "value": row["tag_name"],
            "label": row["tag_name"],
            "set": None,
            "cn": None,
            "scryfall_id": None,
            "card_count": row["card_count_expected"],
        }
        for row in cur.fetchall()
    ]

    combined = card_results + otag_results
    return combined[:limit]


# ---------------------------------------------------------------------------
# /api/otags/neighborhood
# ---------------------------------------------------------------------------

def _otag_card_count(conn: sqlite3.Connection, tag: str) -> int:
    """Live count from `tags`, falling back to tag_catalog.card_count_expected."""
    row = conn.execute(
        "SELECT card_count_expected FROM tag_catalog WHERE tag_name = ?",
        (tag,),
    ).fetchone()
    if row and row["card_count_expected"] is not None:
        return int(row["card_count_expected"])
    cur = conn.execute(
        "SELECT COUNT(*) AS c FROM tags WHERE tag_name = ?",
        (tag,),
    ).fetchone()
    return int(cur["c"]) if cur else 0


def _otag_exists(conn: sqlite3.Connection, tag: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM tag_catalog WHERE tag_name = ? LIMIT 1",
        (tag,),
    ).fetchone()
    return row is not None


def build_neighborhood(
    conn: sqlite3.Connection,
    center: str,
    depth: int = 1,
    types: Iterable[str] = DEFAULT_NEIGHBORHOOD_TYPES,
    max_nodes: int = 60,
) -> Optional[dict]:
    """BFS subgraph around `center`. Returns None if center is unknown.

    Algorithm:
      1. BFS from center along the requested relation types only.
      2. For each visited node, look at outgoing edges in BOTH directions
         (synonym + co_occurs are stored bidirectionally; hierarchy +
         implies are directional but we still traverse both ways for
         exploration).
      3. Stop expanding past `depth` hops.
      4. After BFS, fetch all edges between any pair of visited nodes
         (closes triangles for layout sanity).
      5. If the visited set exceeds max_nodes, prune nodes at the deepest
         depth (lowest card_count first).
    """
    if not _otag_exists(conn, center):
        return None

    types_list = [t for t in types if t in VALID_RELATION_TYPES]
    if not types_list:
        types_list = list(DEFAULT_NEIGHBORHOOD_TYPES)
    placeholders = ",".join("?" * len(types_list))

    depths: dict[str, int] = {center: 0}
    queue: deque[tuple[str, int]] = deque([(center, 0)])

    while queue:
        node, d = queue.popleft()
        if d >= depth:
            continue
        # Outgoing
        cur = conn.execute(
            f"""SELECT dst_otag AS other
                  FROM otag_relations
                 WHERE src_otag = ? AND relation_type IN ({placeholders})""",
            (node, *types_list),
        )
        neighbors = {r["other"] for r in cur.fetchall()}
        # Incoming
        cur = conn.execute(
            f"""SELECT src_otag AS other
                  FROM otag_relations
                 WHERE dst_otag = ? AND relation_type IN ({placeholders})""",
            (node, *types_list),
        )
        neighbors.update(r["other"] for r in cur.fetchall())

        for nb in neighbors:
            if nb in depths:
                continue
            depths[nb] = d + 1
            queue.append((nb, d + 1))

    # Prune to max_nodes if needed: drop deepest first, then lowest card-count
    if len(depths) > max_nodes:
        # Compute counts for tiebreak only on the deepest-tier candidates.
        max_d = max(depths.values())
        # Build prunable list (never prune center)
        sorted_nodes = sorted(
            (n for n in depths if n != center),
            key=lambda n: (-depths[n], _otag_card_count(conn, n)),
        )
        # Keep removing from the front (deepest, lowest count) until under cap
        while len(depths) > max_nodes and sorted_nodes:
            victim = sorted_nodes.pop(0)
            depths.pop(victim, None)
            # Recompute max_d eventually unimportant; loop terminates
            _ = max_d  # noqa: silence unused

    visited = list(depths.keys())
    visited_set = set(visited)

    # Fetch all edges between visited nodes (any of the requested types)
    if visited:
        marks = ",".join("?" * len(visited))
        cur = conn.execute(
            f"""SELECT src_otag, dst_otag, relation_type, weight
                  FROM otag_relations
                 WHERE src_otag IN ({marks})
                   AND dst_otag IN ({marks})
                   AND relation_type IN ({placeholders})""",
            (*visited, *visited, *types_list),
        )
        edge_rows = cur.fetchall()
    else:
        edge_rows = []

    edges = []
    seen_edges: set[tuple] = set()
    for r in edge_rows:
        src = r["src_otag"]
        dst = r["dst_otag"]
        rt = r["relation_type"]
        if src not in visited_set or dst not in visited_set:
            continue
        # Dedupe symmetric pairs for synonym/co_occurs
        if rt in ("synonym", "co_occurs"):
            key = (rt, frozenset((src, dst)))
        else:
            key = (rt, src, dst)
        if key in seen_edges:
            continue
        seen_edges.add(key)
        edges.append({
            "src": src,
            "dst": dst,
            "type": rt,
            "weight": float(r["weight"]) if r["weight"] is not None else 1.0,
        })

    # Batch-fetch descriptions for every visited node in one query.
    # Most descriptions will be NULL until the Tagger description scrape
    # has run; that's fine — clients fall back to example cards.
    descriptions: dict[str, Optional[str]] = {}
    if visited:
        marks = ",".join("?" * len(visited))
        d_cur = conn.execute(
            f"SELECT tag_name, description FROM tag_catalog "
            f"WHERE tag_name IN ({marks})",
            visited,
        )
        for r in d_cur.fetchall():
            descriptions[r["tag_name"]] = r["description"] or None

    nodes = []
    for n in visited:
        nodes.append({
            "id": n,
            "label": n,
            "card_count": _otag_card_count(conn, n),
            "description": descriptions.get(n),
            "depth": depths[n],
            "is_center": n == center,
        })

    return {"center": center, "nodes": nodes, "edges": edges}


# ---------------------------------------------------------------------------
# /api/otags/by-card
# ---------------------------------------------------------------------------

def _resolve_card(
    cards_data: list,
    card_data_by_id: dict,
    scryfall_id: Optional[str],
    name: Optional[str],
    set_code: Optional[str],
    cn: Optional[str],
) -> Optional[dict]:
    """Locate a Scryfall card from id, name+set+cn, or just name."""
    if scryfall_id:
        c = card_data_by_id.get(scryfall_id)
        if c is not None:
            return c

    if not name:
        return None
    nm = name.strip().lower()
    set_l = (set_code or "").strip().lower() or None
    cn_l = (cn or "").strip().lower() or None

    fallback = None
    for c in cards_data:
        if c.get("lang") != "en" or "paper" not in (c.get("games") or []):
            continue
        if (c.get("name") or "").lower() != nm:
            continue
        if set_l and (c.get("set") or "").lower() != set_l:
            continue
        if cn_l and (c.get("collector_number") or "").lower() != cn_l:
            continue
        # First match wins for set+cn; otherwise keep walking
        if set_l or cn_l:
            return c
        if fallback is None:
            fallback = c
    return fallback


def _walk_hierarchy_ancestors(
    conn: sqlite3.Connection, otag: str, max_depth: int = 16
) -> list[str]:
    """Walk hierarchy edges upward from otag, picking the longest chain
    when multiple parents exist (more specific lineage wins).
    """
    seen_global: set[str] = set()

    def _walk(node: str, visited: tuple[str, ...]) -> list[str]:
        if len(visited) > max_depth:
            return list(visited[1:])
        cur = conn.execute(
            """SELECT dst_otag FROM otag_relations
                WHERE src_otag = ? AND relation_type = 'hierarchy'""",
            (node,),
        )
        parents = [r["dst_otag"] for r in cur.fetchall() if r["dst_otag"] not in visited]
        if not parents:
            return list(visited[1:])  # exclude the start node
        best: list[str] = []
        for p in parents:
            chain = _walk(p, visited + (p,))
            if len(chain) > len(best):
                best = chain
        return best

    chain = _walk(otag, (otag,))
    # Defensive dedupe (in case of cycles)
    out = []
    for x in chain:
        if x in seen_global:
            continue
        seen_global.add(x)
        out.append(x)
    return out


def by_card(
    conn: sqlite3.Connection,
    cards_data: list,
    card_data_by_id: dict,
    scryfall_id: Optional[str] = None,
    name: Optional[str] = None,
    set_code: Optional[str] = None,
    cn: Optional[str] = None,
) -> Optional[dict]:
    """Return all otags applied to a card plus each tag's hierarchy ancestors.

    Returns None if the card cannot be resolved. If the card has no otags,
    returns the card payload with otags=[] (the frontend handles this).
    """
    card = _resolve_card(cards_data, card_data_by_id, scryfall_id, name, set_code, cn)
    if card is None:
        return None

    oracle_id = card.get("oracle_id")
    image_uris = card.get("image_uris") or {}
    image_url = (
        image_uris.get("normal")
        or image_uris.get("large")
        or image_uris.get("small")
        or image_uris.get("png")
    )
    if not image_url:
        faces = card.get("card_faces") or []
        if faces:
            face_uris = (faces[0] or {}).get("image_uris") or {}
            image_url = (
                face_uris.get("normal")
                or face_uris.get("large")
                or face_uris.get("small")
                or face_uris.get("png")
            )

    otags_out: list[dict] = []
    if oracle_id:
        cur = conn.execute(
            "SELECT DISTINCT tag_name FROM tags WHERE oracle_id = ?",
            (oracle_id,),
        )
        for row in cur.fetchall():
            tag = row["tag_name"]
            otags_out.append({
                "otag": tag,
                "card_count": _otag_card_count(conn, tag),
                "ancestors": _walk_hierarchy_ancestors(conn, tag),
            })
        # Stable sort by name for deterministic layout
        otags_out.sort(key=lambda o: o["otag"])

    return {
        "card": {
            "name": card.get("name"),
            "set": card.get("set"),
            "cn": card.get("collector_number"),
            "image_url": image_url,
            "scryfall_id": card.get("id"),
        },
        "otags": otags_out,
    }


# ---------------------------------------------------------------------------
# /api/otags/clusters
# ---------------------------------------------------------------------------

def get_clusters(conn: sqlite3.Connection, max_clusters: int = 20) -> dict:
    """Return precomputed Louvain clusters for Atlas mode.

    If no cluster_ids have been populated yet, returns the empty payload
    with a warning string; the frontend uses this to prompt the user to
    run web_enrichment.compute_otag_clusters.
    """
    has_clusters = conn.execute(
        """SELECT 1 FROM tag_catalog
            WHERE tag_type='function' AND cluster_id IS NOT NULL LIMIT 1"""
    ).fetchone()
    if not has_clusters:
        return {
            "clusters": [],
            "tag_to_cluster": {},
            "warning": "Run compute_otag_clusters first",
        }

    cur = conn.execute(
        """SELECT cluster_id, COUNT(*) AS sz
             FROM tag_catalog
            WHERE tag_type='function' AND cluster_id IS NOT NULL
         GROUP BY cluster_id
         ORDER BY sz DESC
            LIMIT ?""",
        (max_clusters,),
    )
    cluster_rows = cur.fetchall()

    clusters_out = []
    tag_to_cluster: dict[str, int] = {}

    for row in cluster_rows:
        cid = int(row["cluster_id"])
        size = int(row["sz"])
        tags_cur = conn.execute(
            """SELECT tag_name, card_count_expected
                 FROM tag_catalog
                WHERE tag_type='function' AND cluster_id = ?
             ORDER BY
                 CASE WHEN card_count_expected IS NULL THEN 1 ELSE 0 END,
                 card_count_expected DESC,
                 tag_name ASC""",
            (cid,),
        )
        tag_rows = tags_cur.fetchall()
        top_tags = [r["tag_name"] for r in tag_rows[:10]]
        label = top_tags[0] if top_tags else f"cluster-{cid}"
        clusters_out.append({
            "id": cid,
            "label": label,
            "size": size,
            "top_tags": top_tags,
        })
        for r in tag_rows:
            tag_to_cluster[r["tag_name"]] = cid

    return {"clusters": clusters_out, "tag_to_cluster": tag_to_cluster}


# ---------------------------------------------------------------------------
# /api/otags/examples
# ---------------------------------------------------------------------------
#
# Why we need this:
#   The Scryfall Tagger docs page lists tag names with no inline
#   descriptions. Per-tag pages on tagger.scryfall.com are auth-walled
#   (CSRF token required). The most useful "what does this otag mean"
#   signal we CAN expose without scraping is a few popular cards that
#   carry the tag — operators can read those names and instantly see
#   the meaning ("oh, wrath-of-god is the wrath family — Wrath of God,
#   Damnation, Toxic Deluge").
#
# Selection: cards joined from the local Scryfall data (cards.CARDS_DATA)
# by oracle_id, filtered to English paper printings, sorted by
# edhrec_rank ascending so the most-recognisable cards come first.

# Lazy module-level oracle_id index. Built on first call so we don't
# pay the construction cost at import time. Rebuilt if cards.reload_card_data
# bumps the underlying CARDS_DATA reference.
_oracle_to_cards_index: Optional[dict] = None
_oracle_index_built_for: Optional[int] = None  # id() of the cards_data list


def _build_oracle_to_cards_index(cards_data: list) -> dict:
    """oracle_id -> [card_dict, ...] sorted by edhrec_rank asc.

    Filters to English paper printings — non-paper / non-English entries
    are unhelpful as examples.
    """
    idx: dict[str, list] = {}
    for c in cards_data:
        oid = c.get("oracle_id")
        if not oid:
            continue
        if c.get("lang") != "en":
            continue
        if "paper" not in (c.get("games") or []):
            continue
        idx.setdefault(oid, []).append(c)
    for oid, lst in idx.items():
        lst.sort(key=lambda c: (c.get("edhrec_rank") or 9_999_999,
                                 c.get("released_at") or ""))
    return idx


def _oracle_index(cards_data: list) -> dict:
    global _oracle_to_cards_index, _oracle_index_built_for
    cur_id = id(cards_data)
    if (_oracle_to_cards_index is None
            or _oracle_index_built_for != cur_id):
        _oracle_to_cards_index = _build_oracle_to_cards_index(cards_data)
        _oracle_index_built_for = cur_id
    return _oracle_to_cards_index


def get_examples_for_otags(
    enrichment_conn: sqlite3.Connection,
    cards_data: list,
    otags: Iterable[str],
    n: int = 3,
) -> dict[str, list[dict]]:
    """For each otag, return the top-N most-popular example cards.

    Output:
        {otag: [{"name": str, "set": str, "cn": str,
                 "scryfall_id": str, "oracle_id": str}, ...], ...}

    Empty list when no English paper printing exists for any oracle_id
    in that otag (rare; happens for a handful of catalogue entries that
    didn't return cards from the search API).

    Performance: per-otag cost is O(distinct_oracle_ids_for_that_otag);
    the global oracle_id -> cards index is built once and cached.
    """
    if n <= 0 or not otags:
        return {}

    otag_list = list(dict.fromkeys(otags))  # dedupe, preserve order
    if not otag_list:
        return {}

    placeholders = ",".join("?" for _ in otag_list)
    rows = enrichment_conn.execute(
        f"""SELECT tag_name, oracle_id FROM tags
             WHERE tag_name IN ({placeholders})""",
        otag_list,
    ).fetchall()

    by_tag: dict[str, list[str]] = {t: [] for t in otag_list}
    for r in rows:
        tag = r[0] if isinstance(r, tuple) else r["tag_name"]
        oid = r[1] if isinstance(r, tuple) else r["oracle_id"]
        by_tag.setdefault(tag, []).append(oid)

    idx = _oracle_index(cards_data)
    out: dict[str, list[dict]] = {}
    for tag, oids in by_tag.items():
        seen_names: set[str] = set()
        candidates: list[dict] = []
        for oid in oids:
            cards_for_oid = idx.get(oid)
            if not cards_for_oid:
                continue
            best = cards_for_oid[0]  # best by edhrec_rank
            nm = best.get("name") or ""
            if nm in seen_names:
                continue
            seen_names.add(nm)
            candidates.append(best)
        # Sort across-tag: lowest edhrec_rank first (most popular)
        candidates.sort(key=lambda c: (c.get("edhrec_rank") or 9_999_999))
        out[tag] = [
            {
                "name": c.get("name"),
                "set": c.get("set"),
                "cn": c.get("collector_number"),
                "scryfall_id": c.get("id"),
                "oracle_id": c.get("oracle_id"),
                "edhrec_rank": c.get("edhrec_rank"),
            }
            for c in candidates[:n]
        ]
    return out
