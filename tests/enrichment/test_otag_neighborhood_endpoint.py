# tests/enrichment/test_otag_neighborhood_endpoint.py
# ---------------------------------------------------------------------------
# Tests for the BFS neighborhood helper. Synthesises an otag_relations
# graph in a tmp DB and exercises depth, type filter, and pruning.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_enrichment.otag_explorer import build_neighborhood


def _seed(conn, edges, catalog=None):
    """edges: [(src, dst, rel_type, weight)]; catalog: [(name, count)]"""
    rows = []
    for src, dst, rt, w in edges:
        rows.append((src, dst, rt, w, 'test', '2026-01-01'))
    conn.executemany(
        """INSERT OR REPLACE INTO otag_relations
           (src_otag, dst_otag, relation_type, weight, source, last_updated)
           VALUES (?,?,?,?,?,?)""",
        rows,
    )
    if catalog is None:
        # Auto-derive catalog from edges
        names = set()
        for s, d, _, _ in edges:
            names.add(s); names.add(d)
        catalog = [(n, 100) for n in names]
    conn.executemany(
        """INSERT OR REPLACE INTO tag_catalog
           (tag_name, tag_type, parent, description,
            card_count_expected, source)
           VALUES (?, 'function', NULL, NULL, ?, 'test')""",
        catalog,
    )
    conn.commit()


def test_unknown_center_returns_none(tmp_enrichment_db):
    _seed(tmp_enrichment_db, [], catalog=[])
    assert build_neighborhood(tmp_enrichment_db, 'no-such-tag') is None


def test_depth_one_includes_only_direct_neighbors(tmp_enrichment_db):
    edges = [
        ('a', 'b', 'co_occurs', 0.8),
        ('b', 'c', 'co_occurs', 0.7),
    ]
    _seed(tmp_enrichment_db, edges)
    res = build_neighborhood(tmp_enrichment_db, 'a', depth=1)
    ids = {n['id'] for n in res['nodes']}
    assert ids == {'a', 'b'}
    assert 'c' not in ids


def test_depth_two_walks_two_hops(tmp_enrichment_db):
    edges = [
        ('a', 'b', 'co_occurs', 0.8),
        ('b', 'c', 'co_occurs', 0.7),
        ('c', 'd', 'co_occurs', 0.6),
    ]
    _seed(tmp_enrichment_db, edges)
    res = build_neighborhood(tmp_enrichment_db, 'a', depth=2)
    ids = {n['id'] for n in res['nodes']}
    assert {'a', 'b', 'c'} <= ids
    assert 'd' not in ids


def test_type_filter_excludes_other_relations(tmp_enrichment_db):
    edges = [
        ('a', 'b', 'co_occurs', 0.8),
        ('a', 'c', 'sibling_disjoint', 1.0),
    ]
    _seed(tmp_enrichment_db, edges)
    res = build_neighborhood(tmp_enrichment_db, 'a', depth=1, types=['co_occurs'])
    ids = {n['id'] for n in res['nodes']}
    assert 'b' in ids and 'c' not in ids


def test_max_nodes_prunes_deepest_first(tmp_enrichment_db):
    # Center connects to 3 depth-1 neighbors; each depth-1 has its own
    # depth-2 neighbor. Limit to 4 nodes → all depth-2's should be pruned
    # before any depth-1.
    edges = [
        ('a', 'b1', 'co_occurs', 0.8),
        ('a', 'b2', 'co_occurs', 0.8),
        ('a', 'b3', 'co_occurs', 0.8),
        ('b1', 'c1', 'co_occurs', 0.5),
        ('b2', 'c2', 'co_occurs', 0.5),
        ('b3', 'c3', 'co_occurs', 0.5),
    ]
    _seed(tmp_enrichment_db, edges)
    res = build_neighborhood(tmp_enrichment_db, 'a', depth=2, max_nodes=4)
    ids = {n['id'] for n in res['nodes']}
    assert 'a' in ids
    # All four kept nodes should be depth 0 or 1
    for n in res['nodes']:
        assert n['depth'] <= 1


def test_triangle_closure_includes_inter_neighbor_edges(tmp_enrichment_db):
    # a connects to b and c; b and c also connect.
    # Depth=1 BFS visits {a, b, c}; the b<->c edge must appear in the
    # final edge list (closes the triangle).
    edges = [
        ('a', 'b', 'co_occurs', 0.8),
        ('a', 'c', 'co_occurs', 0.8),
        ('b', 'c', 'co_occurs', 0.5),
    ]
    _seed(tmp_enrichment_db, edges)
    res = build_neighborhood(tmp_enrichment_db, 'a', depth=1, types=['co_occurs'])
    ids = {n['id'] for n in res['nodes']}
    assert ids == {'a', 'b', 'c'}
    edge_pairs = set()
    for e in res['edges']:
        edge_pairs.add(frozenset((e['src'], e['dst'])))
    assert frozenset(('b', 'c')) in edge_pairs


def test_incoming_edges_traversed(tmp_enrichment_db):
    # Hierarchy edge points b -> a (a is the parent). BFS from a should
    # still find b by following incoming hierarchy edges.
    edges = [
        ('b', 'a', 'hierarchy', 1.0),
    ]
    _seed(tmp_enrichment_db, edges)
    res = build_neighborhood(tmp_enrichment_db, 'a', depth=1, types=['hierarchy'])
    ids = {n['id'] for n in res['nodes']}
    assert {'a', 'b'} == ids
