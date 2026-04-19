# tests/integrations/test_preset_store.py
# ---------------------------------------------------------------------------
# Unit tests for Phase 0B-1 unified preset store:
#   * builtin presets present and well-formed
#   * sort_configs/*.txt auto-import as read-only file presets
#   * user presets round-trip through save_user_preset / list / delete
#   * resolve_preset handles legacy names ("color", "price", etc.)
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import preset_store


class TestBuiltins(unittest.TestCase):

    def test_all_builtins_present(self):
        ids = {p["id"] for p in preset_store.list_builtin_presets()}
        for expected in {
            "builtin:color",
            "builtin:mana_value",
            "builtin:set",
            "builtin:price",
            "builtin:type",
        }:
            self.assertIn(expected, ids)

    def test_builtins_are_read_only(self):
        for p in preset_store.list_builtin_presets():
            self.assertFalse(p["editable"])
            self.assertEqual(p["source"], "builtin")
            self.assertIsInstance(p["bins"], list)
            self.assertGreater(len(p["bins"]), 0)
            for b in p["bins"]:
                self.assertIn("bin", b)
                self.assertIn("query", b)
                self.assertIn("description", b)

    def test_resolve_legacy_names(self):
        self.assertEqual(preset_store.resolve_preset("color")["id"], "builtin:color")
        self.assertEqual(preset_store.resolve_preset("price")["id"], "builtin:price")
        self.assertEqual(preset_store.resolve_preset("cmc")["id"], "builtin:mana_value")

    def test_resolve_unknown_returns_none(self):
        self.assertIsNone(preset_store.resolve_preset("definitely_not_a_preset"))


class TestFilePresets(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "legacy_color.txt").write_text(
            "# test preset\n"
            "bins: 3\n"
            "fallback: 3\n"
            "bin1: c:w\n"
            "bin2: c:u\n",
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_preset_loaded(self):
        presets = preset_store.list_file_presets(configs_dir=str(self.tmp))
        self.assertEqual(len(presets), 1)
        p = presets[0]
        self.assertEqual(p["source"], "file")
        self.assertFalse(p["editable"])
        self.assertEqual(p["bin_count"], 3)
        self.assertEqual(p["fallback_bin"], 3)
        self.assertEqual(len(p["bins"]), 2)
        self.assertEqual(p["bins"][0]["query"], "c:w")


class TestUserPresets(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_and_list_roundtrip(self):
        saved = preset_store.save_user_preset(
            {
                "name": "My Deck",
                "description": "for testing",
                "bin_count": 2,
                "fallback_bin": 2,
                "bins": [
                    {"bin": 1, "query": "c:w", "description": "whites"},
                    {"bin": 2, "query": "c:u", "description": "blues"},
                ],
            },
            user_dir=str(self.tmp),
        )
        self.assertEqual(saved["source"], "user")
        self.assertTrue(saved["editable"])
        self.assertEqual(saved["id"], "user:my_deck")

        listed = preset_store.list_user_presets(user_dir=str(self.tmp))
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["name"], "My Deck")
        self.assertEqual(listed[0]["bins"][0]["query"], "c:w")

    def test_delete(self):
        preset_store.save_user_preset(
            {"name": "tmp", "bin_count": 1, "fallback_bin": 1,
             "bins": [{"bin": 1, "query": "*"}]},
            user_dir=str(self.tmp),
        )
        self.assertTrue(
            preset_store.delete_user_preset("user:tmp", user_dir=str(self.tmp))
        )
        self.assertEqual(preset_store.list_user_presets(user_dir=str(self.tmp)), [])

    def test_delete_nonexistent(self):
        self.assertFalse(
            preset_store.delete_user_preset("user:nope", user_dir=str(self.tmp))
        )

    def test_delete_rejects_non_user_ids(self):
        self.assertFalse(preset_store.delete_user_preset("builtin:color"))
        self.assertFalse(preset_store.delete_user_preset("file:something"))


class TestCatalog(unittest.TestCase):

    def setUp(self):
        self.cfg = Path(tempfile.mkdtemp())
        self.usr = Path(tempfile.mkdtemp())
        (self.cfg / "color_type.txt").write_text(
            "bins: 2\nfallback: 2\nbin1: c:w\n",
            encoding="utf-8",
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.cfg, ignore_errors=True)
        shutil.rmtree(self.usr, ignore_errors=True)

    def test_list_presets_combines_sources(self):
        preset_store.save_user_preset(
            {"name": "mine", "bin_count": 1, "fallback_bin": 1,
             "bins": [{"bin": 1, "query": "*"}]},
            user_dir=str(self.usr),
        )
        all_ = preset_store.list_presets(
            configs_dir=str(self.cfg), user_dir=str(self.usr)
        )
        sources = {p["source"] for p in all_}
        self.assertIn("builtin", sources)
        self.assertIn("file", sources)
        self.assertIn("user", sources)


if __name__ == "__main__":
    unittest.main()
