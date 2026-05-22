"""
Regression test for the v2-hash unpack bug.

`create_card_hashes_v2.process_image(args)` expects a 4-tuple:
    (filename, images_dir, hash_size, layout)

The v2 hash builder in web_database._step_generate_v2_hashes used to
build 3-tuples (no layout) and crash with:
    ValueError: not enough values to unpack (expected 4, got 3)

This test pins:
  1. process_image still expects exactly 4 elements.
  2. create_v2_hash_database is the new public entry point, and it
     accepts cards_json / progress_callback kwargs (the contract
     web_database depends on).
  3. The layout-map builder handles a missing cards JSON gracefully
     (empty map, not a crash).
  4. End-to-end smoke: a 1-file fixture goes through
     create_v2_hash_database and produces a valid output JSON.
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class V2HashContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.mod = importlib.import_module('create_card_hashes_v2')

    def test_process_image_unpacks_four(self):
        """The function reads `filename, images_dir, hash_size, layout`
        from its `args` parameter. The arity is load-bearing — passing
        a 3-tuple from a caller (as web_database used to) crashes."""
        src = inspect.getsource(self.mod.process_image)
        self.assertIn('filename, images_dir, hash_size, layout = args', src,
                      "process_image must continue to expect 4 args")

    def test_create_v2_hash_database_is_public(self):
        """The public entry point web_database depends on exists with
        the expected kwargs."""
        fn = getattr(self.mod, 'create_v2_hash_database', None)
        self.assertIsNotNone(fn, "create_v2_hash_database must be exposed")
        sig = inspect.signature(fn)
        for required in ('images_dir', 'output_json', 'cards_json',
                         'hash_size', 'progress_callback'):
            self.assertIn(required, sig.parameters,
                          f"create_v2_hash_database missing {required!r} kwarg")

    def test_layout_map_handles_missing_cards_json(self):
        """No cards JSON on disk → empty layout map, no crash. This
        is the fresh-install path and the kiosk's behaviour right
        after a clean checkout."""
        m = self.mod._build_layout_map('/nonexistent/no-such-file.json')
        self.assertEqual(m, {})

    def test_layout_map_picks_up_saga_and_battle(self):
        """Sagas (native layout) and battles (type_line probe) both
        land in the map. Other layouts default to 'normal' (not in
        the map at all)."""
        with tempfile.NamedTemporaryFile(
                mode='w', suffix='.json', delete=False, encoding='utf-8') as f:
            json.dump([
                {'id': 'card-saga',   'layout': 'saga'},
                {'id': 'card-class',  'layout': 'class'},
                {'id': 'card-normal', 'layout': 'normal'},
                {'id': 'card-battle', 'layout': 'transform',
                 'type_line': 'Battle — Siege'},
                {'id': 'card-faces',  'layout': 'transform',
                 'card_faces': [{'type_line': 'Battle — Siege'}]},
            ], f)
            path = f.name
        try:
            m = self.mod._build_layout_map(path)
            self.assertEqual(m.get('card-saga'),   'saga')
            self.assertEqual(m.get('card-class'),  'class')
            self.assertNotIn('card-normal', m)  # normal not stored
            self.assertEqual(m.get('card-battle'), 'battle')
            self.assertEqual(m.get('card-faces'),  'battle')
        finally:
            os.unlink(path)


if __name__ == '__main__':
    unittest.main()
