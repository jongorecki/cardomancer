# tests/enrichment/test_derive_otag_relations.py
# ---------------------------------------------------------------------------
# Tests for the otag relation derivation pipeline.
#
# Uses a synthetic tags table to exercise each relation type and the
# transitive reduction logic.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import enrichment_db
from web_enrichment.derive_otag_relations import (
    REL_CO_OCCURS,
    REL_HIERARCHY,
    REL_IMPLIES,
    REL_RELATED,
    REL_SIBLING_DISJOINT,
    REL_SYNONYM,
    derive_all,
    _transitive_reduction,
)


def _mk_db() -> tuple[str, str]:
    """Return (tmpdir, db_path) with schema created."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "test.db")
    conn = enrichment_db.get_connection(db_path=path)
    conn.close()
    return tmp, path


def _seed_tags(conn: sqlite3.Connection, tag_oracle_map: dict[str, list[str]],
               tag_type: str = "function") -> None:
    """Seed tags table + tag_catalog entries from a dict of {tag: [oracle_ids]}."""
    tag_rows = []
    catalog_rows = []
    for tag, oracle_ids in tag_oracle_map.items():
        for oid in oracle_ids:
            tag_rows.append((oid, tag, "test"))
        catalog_rows.append((tag, tag_type, None, None, len(oracle_ids), "test"))

    conn.executemany(
        "INSERT OR IGNORE INTO tags (oracle_id, tag_name, source) VALUES (?,?,?)",
        tag_rows,
    )
    conn.executemany(
        """INSERT OR REPLACE INTO tag_catalog
           (tag_name, tag_type, parent, description, card_count_expected, source)
           VALUES (?,?,?,?,?,?)""",
        catalog_rows,
    )
    conn.commit()


def _get_relations(conn: sqlite3.Connection,
                   rel_type: str) -> set[tuple[str, str]]:
    """Return (src, dst) pairs for a given relation_type."""
    rows = conn.execute(
        "SELECT src_otag, dst_otag FROM otag_relations "
        "WHERE relation_type=? AND source='derived'",
        (rel_type,)
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


class TestImpliesAndHierarchy(unittest.TestCase):
    """wrath ⊂ removal (5x more cards) → implies + hierarchy."""

    def setUp(self):
        self.tmp, self.db_path = _mk_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # wrath cards: 10; removal cards: 50 (includes the 10)
        wrath_ids = [f"wrath-{i}" for i in range(10)]
        removal_ids = wrath_ids + [f"removal-only-{i}" for i in range(40)]
        _seed_tags(conn, {
            "wrath": wrath_ids,
            "removal": removal_ids,
        })
        conn.close()
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_implies_wrath_removal(self):
        derive_all(self.conn)
        edges = _get_relations(self.conn, REL_IMPLIES)
        self.assertIn(("wrath", "removal"), edges,
                      f"Expected implies(wrath, removal). Got: {edges}")

    def test_hierarchy_wrath_removal(self):
        derive_all(self.conn)
        edges = _get_relations(self.conn, REL_HIERARCHY)
        self.assertIn(("wrath", "removal"), edges,
                      f"Expected hierarchy(wrath, removal). Got: {edges}")

    def test_parent_set_in_catalog(self):
        derive_all(self.conn)
        row = self.conn.execute(
            "SELECT parent FROM tag_catalog WHERE tag_name='wrath'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "removal",
                         f"Expected parent='removal' for wrath, got {row[0]}")


class TestSynonym(unittest.TestCase):
    """wrath and mass-removal with 100% overlap → synonym both directions."""

    def setUp(self):
        self.tmp, self.db_path = _mk_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        ids = [f"card-{i}" for i in range(20)]
        _seed_tags(conn, {
            "wrath": ids,
            "mass-removal": ids,
        })
        conn.close()
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_synonym_both_directions(self):
        derive_all(self.conn)
        edges = _get_relations(self.conn, REL_SYNONYM)
        self.assertIn(("wrath", "mass-removal"), edges)
        self.assertIn(("mass-removal", "wrath"), edges)

    def test_implies_not_written_for_synonyms(self):
        """Synonyms (equal sets) must not produce implies edges."""
        derive_all(self.conn)
        implies = _get_relations(self.conn, REL_IMPLIES)
        # Both directions should not be written for equal sets
        self.assertNotIn(("wrath", "mass-removal"), implies,
                         "implies should not be written when sets are equal")
        self.assertNotIn(("mass-removal", "wrath"), implies)


class TestNoRelationForDisjointSets(unittest.TestCase):
    """creature and instant with 0% overlap → no relation written."""

    def setUp(self):
        self.tmp, self.db_path = _mk_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        creature_ids = [f"creature-{i}" for i in range(30)]
        instant_ids = [f"instant-{i}" for i in range(30)]
        _seed_tags(conn, {
            "creature": creature_ids,
            "instant": instant_ids,
        })
        conn.close()
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_relation_for_disjoint(self):
        derive_all(self.conn)
        all_rows = self.conn.execute(
            "SELECT src_otag, dst_otag, relation_type FROM otag_relations "
            "WHERE source='derived'"
        ).fetchall()
        pairs = {(r[0], r[1]) for r in all_rows}
        # No relation between creature and instant (and vice versa)
        self.assertNotIn(("creature", "instant"), pairs)
        self.assertNotIn(("instant", "creature"), pairs)


class TestManaRockImpliesRamp(unittest.TestCase):
    """mana-rock ⊂ ramp, ramp much larger → implies(mana-rock, ramp) + hierarchy."""

    def setUp(self):
        self.tmp, self.db_path = _mk_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        mana_rock_ids = [f"manarock-{i}" for i in range(20)]
        # ramp = mana_rock + 80 more
        ramp_ids = mana_rock_ids + [f"ramp-only-{i}" for i in range(80)]
        _seed_tags(conn, {
            "mana-rock": mana_rock_ids,
            "ramp": ramp_ids,
        })
        conn.close()
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_implies_mana_rock_ramp(self):
        derive_all(self.conn)
        edges = _get_relations(self.conn, REL_IMPLIES)
        self.assertIn(("mana-rock", "ramp"), edges)

    def test_hierarchy_mana_rock_ramp(self):
        derive_all(self.conn)
        edges = _get_relations(self.conn, REL_HIERARCHY)
        self.assertIn(("mana-rock", "ramp"), edges)

    def test_implies_not_reversed(self):
        """ramp does NOT imply mana-rock (ramp is bigger)."""
        derive_all(self.conn)
        edges = _get_relations(self.conn, REL_IMPLIES)
        self.assertNotIn(("ramp", "mana-rock"), edges)


class TestSiblingDisjoint(unittest.TestCase):
    """wrath and spot-removal both under removal, no card overlap → sibling_disjoint."""

    def setUp(self):
        self.tmp, self.db_path = _mk_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        wrath_ids = [f"wrath-{i}" for i in range(10)]
        spot_ids = [f"spot-{i}" for i in range(10)]
        removal_ids = wrath_ids + spot_ids
        _seed_tags(conn, {
            "wrath": wrath_ids,
            "spot-removal": spot_ids,
            "removal": removal_ids,
        })
        conn.close()
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sibling_disjoint(self):
        derive_all(self.conn)
        edges = _get_relations(self.conn, REL_SIBLING_DISJOINT)
        # Both directions should be written
        found = (
            ("wrath", "spot-removal") in edges or
            ("spot-removal", "wrath") in edges
        )
        self.assertTrue(found,
                        f"Expected sibling_disjoint between wrath and spot-removal. "
                        f"Got: {edges}")

    def test_sibling_disjoint_symmetric(self):
        derive_all(self.conn)
        edges = _get_relations(self.conn, REL_SIBLING_DISJOINT)
        # Both directions should be present
        self.assertIn(("wrath", "spot-removal"), edges)
        self.assertIn(("spot-removal", "wrath"), edges)


class TestTransitiveReduction(unittest.TestCase):
    """wrath ⊂ creature-removal ⊂ removal → only direct edges in hierarchy."""

    def setUp(self):
        self.tmp, self.db_path = _mk_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        wrath_ids = [f"wrath-{i}" for i in range(5)]
        creature_removal_ids = wrath_ids + [f"cr-only-{i}" for i in range(20)]
        removal_ids = creature_removal_ids + [f"removal-only-{i}" for i in range(75)]
        _seed_tags(conn, {
            "wrath": wrath_ids,
            "creature-removal": creature_removal_ids,
            "removal": removal_ids,
        })
        conn.close()
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_direct_edges_present(self):
        derive_all(self.conn)
        hierarchy = _get_relations(self.conn, REL_HIERARCHY)
        self.assertIn(("wrath", "creature-removal"), hierarchy,
                      f"Expected wrath→creature-removal. Got: {hierarchy}")
        self.assertIn(("creature-removal", "removal"), hierarchy,
                      f"Expected creature-removal→removal. Got: {hierarchy}")

    def test_transitive_edge_removed(self):
        """wrath → removal must NOT appear (transitive shortcut removed)."""
        derive_all(self.conn)
        hierarchy = _get_relations(self.conn, REL_HIERARCHY)
        self.assertNotIn(("wrath", "removal"), hierarchy,
                         "Transitive edge wrath→removal should be removed by reduction")

    def test_wrath_parent_is_creature_removal(self):
        derive_all(self.conn)
        row = self.conn.execute(
            "SELECT parent FROM tag_catalog WHERE tag_name='wrath'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "creature-removal",
                         f"Wrath's immediate parent should be creature-removal, "
                         f"got {row[0]}")


class TestTransitiveReductionUnit(unittest.TestCase):
    """Unit test for the _transitive_reduction helper."""

    def test_removes_transitive_edge(self):
        edges = {("A", "B"), ("B", "C"), ("A", "C")}
        reduced = _transitive_reduction(edges)
        self.assertIn(("A", "B"), reduced)
        self.assertIn(("B", "C"), reduced)
        self.assertNotIn(("A", "C"), reduced)

    def test_keeps_all_direct_when_no_transitive(self):
        edges = {("X", "Y"), ("X", "Z")}
        reduced = _transitive_reduction(edges)
        self.assertEqual(reduced, edges)

    def test_empty_input(self):
        self.assertEqual(_transitive_reduction(set()), set())


class TestDeriveAllClearsOldDerived(unittest.TestCase):
    """A second derive_all() run replaces old derived rows cleanly."""

    def setUp(self):
        self.tmp, self.db_path = _mk_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        ids_a = [f"card-{i}" for i in range(10)]
        ids_b = ids_a + [f"card-b-{i}" for i in range(20)]
        _seed_tags(conn, {"tagA": ids_a, "tagB": ids_b})
        conn.close()
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_second_run_same_result(self):
        derive_all(self.conn)
        count1 = self.conn.execute(
            "SELECT COUNT(*) FROM otag_relations WHERE source='derived'"
        ).fetchone()[0]

        derive_all(self.conn)
        count2 = self.conn.execute(
            "SELECT COUNT(*) FROM otag_relations WHERE source='derived'"
        ).fetchone()[0]

        self.assertEqual(count1, count2,
                         "Two consecutive derive_all() calls must produce identical row counts")

    def test_manual_rows_preserved(self):
        """Manual rows must never be deleted by derive_all()."""
        self.conn.execute(
            """INSERT INTO otag_relations
               (src_otag, dst_otag, relation_type, weight, source, last_updated)
               VALUES ('hand-curated-a', 'hand-curated-b', 'implies', 1.0, 'manual', '2026-01-01')"""
        )
        self.conn.commit()

        derive_all(self.conn)

        row = self.conn.execute(
            "SELECT * FROM otag_relations WHERE source='manual'"
        ).fetchone()
        self.assertIsNotNone(row,
                             "Manual rows must survive derive_all()")


class TestCoOccursAndRelated(unittest.TestCase):
    """Tags with partial overlap land in co_occurs or related."""

    def setUp(self):
        self.tmp, self.db_path = _mk_db()
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # Shared: 10 cards. A-only: 10, B-only: 10 → Jaccard 10/30 ≈ 0.33
        shared = [f"shared-{i}" for i in range(10)]
        a_only = [f"a-only-{i}" for i in range(10)]
        b_only = [f"b-only-{i}" for i in range(10)]
        _seed_tags(conn, {
            "tagA": shared + a_only,
            "tagB": shared + b_only,
        })
        conn.close()
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_co_occurs_both_directions(self):
        derive_all(self.conn)
        co = _get_relations(self.conn, REL_CO_OCCURS)
        self.assertIn(("tagA", "tagB"), co)
        self.assertIn(("tagB", "tagA"), co)

    def test_co_occurs_weight_is_jaccard(self):
        derive_all(self.conn)
        row = self.conn.execute(
            "SELECT weight FROM otag_relations "
            "WHERE src_otag='tagA' AND dst_otag='tagB' "
            "AND relation_type='co_occurs'"
        ).fetchone()
        self.assertIsNotNone(row)
        expected_j = 10 / 30
        self.assertAlmostEqual(row[0], expected_j, places=3)

    def test_not_implies_when_partial(self):
        """Partial overlap must not produce implies."""
        derive_all(self.conn)
        implies = _get_relations(self.conn, REL_IMPLIES)
        self.assertNotIn(("tagA", "tagB"), implies)
        self.assertNotIn(("tagB", "tagA"), implies)


if __name__ == "__main__":
    unittest.main()
