# tests/enrichment/test_edhrec.py
# Unit tests for EDHRECSource.

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import enrichment_db
from web_enrichment.edhrec import (
    EDHRECSource, _slug, _lookup,
    ARCHETYPES, _build_commander_archetype_map,
)


class TestEDHRECHelpers(unittest.TestCase):

    def test_slug_basic(self):
        self.assertEqual(_slug("Sol Ring"), "sol-ring")
        self.assertEqual(_slug("Atraxa, Praetors' Voice"), "atraxa-praetors-voice")
        self.assertEqual(_slug("Thassa's Oracle"), "thassas-oracle")
        self.assertEqual(_slug("Edgar Markov"), "edgar-markov")

    def test_lookup_hit(self):
        name_map = {"Sol Ring": "oid-abc"}
        self.assertEqual(_lookup("Sol Ring", name_map), "oid-abc")

    def test_lookup_miss(self):
        self.assertIsNone(_lookup("Unknown Card", {}))


class TestArchetypeCatalogue(unittest.TestCase):

    def test_archetypes_non_empty(self):
        self.assertGreater(len(ARCHETYPES), 10)

    def test_each_archetype_has_commanders(self):
        for name, slugs in ARCHETYPES.items():
            self.assertGreater(len(slugs), 0, f"Archetype {name!r} is empty")

    def test_no_duplicate_slugs_within_archetype(self):
        for name, slugs in ARCHETYPES.items():
            self.assertEqual(len(slugs), len(set(slugs)),
                             f"Archetype {name!r} has duplicate slugs")

    def test_commander_archetype_map_covers_all_slugs(self):
        cmap = _build_commander_archetype_map()
        all_slugs = {s for slugs in ARCHETYPES.values() for s in slugs}
        self.assertEqual(set(cmap.keys()), all_slugs)

    def test_commander_archetype_map_values_are_archetype_names(self):
        cmap = _build_commander_archetype_map()
        for slug, archetypes in cmap.items():
            for arch in archetypes:
                self.assertIn(arch, ARCHETYPES,
                              f"Slug {slug!r} maps to unknown archetype {arch!r}")


class TestEDHRECComputation(unittest.TestCase):

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp, "test.db")
        self.conn = enrichment_db.get_connection(db_path=self.db_path)
        self.source = EDHRECSource()

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_name_map(self) -> dict[str, str]:
        return {
            "Sol Ring": "oid-sol-ring",
            "Counterspell": "oid-counterspell",
            "Birds of Paradise": "oid-bop",
            "Grizzly Bears": "oid-bears",
        }

    def test_universal_staple_from_top_cards(self):
        top_cards = [
            {"name": "Sol Ring", "num_decks": 8000000,
             "potential_decks": 9000000},
        ]
        staple_rows, salt_rows, _ = self.source._compute_enrichment(
            top_cards, {}, {}, self._make_name_map(), []
        )
        oids = [r["oracle_id"] for r in staple_rows if r["tier"] == "universal"]
        self.assertIn("oid-sol-ring", oids)

    def test_below_threshold_not_universal(self):
        top_cards = [
            {"name": "Grizzly Bears",
             "num_decks": 100, "potential_decks": 9000000},
        ]
        staple_rows, _, _ = self.source._compute_enrichment(
            top_cards, {}, {}, self._make_name_map(), []
        )
        oids = [r["oracle_id"] for r in staple_rows if r["tier"] == "universal"]
        self.assertNotIn("oid-bears", oids)

    def test_salt_from_card_page(self):
        card_details = {
            "expropriate": {
                "name": "Expropriate",
                "num_decks": 100000,
                "potential_decks": 9000000,
                "salt": 4.2,
            }
        }
        name_map = {"Expropriate": "oid-exprop", **self._make_name_map()}
        _, salt_rows, _ = self.source._compute_enrichment(
            [], {}, card_details, name_map, []
        )
        salts = {r["oracle_id"]: r["salt"] for r in salt_rows}
        self.assertAlmostEqual(salts.get("oid-exprop"), 4.2, places=1)

    def test_archetype_staple_threshold(self):
        # Card in top-100 of 4 commanders → archetype
        commander_data = {
            f"cmdr-{i}": [
                {"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000},
                {"name": "Counterspell", "num_decks": 50, "potential_decks": 1000},
            ]
            for i in range(5)
        }
        staple_rows, _, _ = self.source._compute_enrichment(
            [], commander_data, {}, self._make_name_map(), []
        )
        archetype_oids = {r["oracle_id"]
                         for r in staple_rows if r["tier"] == "archetype"}
        self.assertIn("oid-sol-ring", archetype_oids)
        self.assertIn("oid-counterspell", archetype_oids)

    def test_archetype_below_min_commanders(self):
        # Card in top-100 of only 2 commanders → not archetype (need >= 3)
        commander_data = {
            f"cmdr-{i}": [
                {"name": "Birds of Paradise",
                 "num_decks": 100, "potential_decks": 1000},
            ]
            for i in range(2)
        }
        staple_rows, _, _ = self.source._compute_enrichment(
            [], commander_data, {}, self._make_name_map(), []
        )
        archetype_oids = {r["oracle_id"]
                         for r in staple_rows if r["tier"] == "archetype"}
        self.assertNotIn("oid-bop", archetype_oids)

    def test_archetype_staple_with_archetype_map(self):
        # 4 commanders all from the "tokens" archetype contain Sol Ring → archetype staple
        c2a = {f"cmdr-{i}": ["tokens"] for i in range(4)}
        commander_data = {
            f"cmdr-{i}": [
                {"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000},
            ]
            for i in range(4)
        }
        staple_rows, _, _ = self.source._compute_enrichment(
            [], commander_data, {}, self._make_name_map(), [],
            commander_to_archetypes=c2a,
        )
        archetype_oids = {r["oracle_id"] for r in staple_rows
                         if r["tier"] == "archetype"}
        self.assertIn("oid-sol-ring", archetype_oids)
        arch_row = next(r for r in staple_rows
                        if r["tier"] == "archetype"
                        and r["oracle_id"] == "oid-sol-ring")
        self.assertIn("tokens", json.loads(arch_row["archetypes_json"]))

    def test_archetype_staple_multi_archetype(self):
        # Sol Ring appears in commanders from both "tokens" and "combo" archetypes
        c2a = {
            "cmdr-0": ["tokens"], "cmdr-1": ["tokens"], "cmdr-2": ["tokens"],
            "cmdr-3": ["combo"],  "cmdr-4": ["combo"],  "cmdr-5": ["combo"],
        }
        commander_data = {
            slug: [{"name": "Sol Ring", "num_decks": 100, "potential_decks": 1000}]
            for slug in c2a
        }
        staple_rows, _, _ = self.source._compute_enrichment(
            [], commander_data, {}, self._make_name_map(), [],
            commander_to_archetypes=c2a,
        )
        arch_row = next(
            (r for r in staple_rows
             if r["tier"] == "archetype" and r["oracle_id"] == "oid-sol-ring"),
            None,
        )
        self.assertIsNotNone(arch_row)
        archetypes = json.loads(arch_row["archetypes_json"])
        self.assertIn("tokens", archetypes)
        self.assertIn("combo", archetypes)

    def test_archetype_below_min_per_archetype(self):
        # Only 2 commanders from "tokens" → does not qualify (need >= 3)
        c2a = {"cmdr-0": ["tokens"], "cmdr-1": ["tokens"]}
        commander_data = {
            slug: [{"name": "Birds of Paradise", "num_decks": 100,
                    "potential_decks": 1000}]
            for slug in c2a
        }
        staple_rows, _, _ = self.source._compute_enrichment(
            [], commander_data, {}, self._make_name_map(), [],
            commander_to_archetypes=c2a,
        )
        archetype_oids = {r["oracle_id"] for r in staple_rows
                         if r["tier"] == "archetype"}
        self.assertNotIn("oid-bop", archetype_oids)

    def test_unknown_card_skipped(self):
        top_cards = [{"name": "Completely Unknown Card",
                      "num_decks": 9000000, "potential_decks": 9000001}]
        warnings: list[str] = []
        staple_rows, _, _ = self.source._compute_enrichment(
            top_cards, {}, {}, self._make_name_map(), warnings
        )
        # Should produce no rows (no oracle_id match), not crash
        self.assertEqual(len(staple_rows), 0)


class TestEDHRECFixture(unittest.TestCase):
    """Validate the real fixture files."""

    def test_commander_fixture_shape(self):
        fpath = (Path(__file__).parent.parent /
                 "fixtures" / "edhrec" / "commander_atraxa.json")
        if not fpath.exists():
            self.skipTest("EDHREC commander fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        cardlists = (data.get("container", {})
                     .get("json_dict", {})
                     .get("cardlists", []))
        self.assertIsInstance(cardlists, list)
        self.assertGreater(len(cardlists), 0)
        if cardlists:
            cv = cardlists[0].get("cardviews", [])
            self.assertIsInstance(cv, list)
            if cv:
                self.assertIn("name", cv[0])

    def test_card_fixture_has_salt(self):
        fpath = (Path(__file__).parent.parent /
                 "fixtures" / "edhrec" / "card_sol_ring.json")
        if not fpath.exists():
            self.skipTest("EDHREC card fixture not present")
        data = json.loads(fpath.read_text(encoding="utf-8"))
        card = (data.get("container", {})
                .get("json_dict", {})
                .get("card", {}))
        self.assertIn("salt", card)
        self.assertIn("num_decks", card)
        self.assertIn("potential_decks", card)


if __name__ == "__main__":
    unittest.main()
