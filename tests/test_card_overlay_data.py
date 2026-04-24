"""Tests for the Sort Session live card-info overlay (Phase 4 item 4.19).

Covers the `_build_card_overlay_info` helper in web_server.py, which is
the pure-function core the /api/card/overlay-info endpoint wraps. We
monkey-patch `cards.CARDS_DATA` with a small fixture list so the tests
don't depend on the full Scryfall bulk cache being loaded.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fixture_cards():
    """Three hand-crafted Scryfall-like card dicts covering the cases
    the overlay helper has to handle: multiple printings, missing price,
    and a transform card with per-face image_uris."""
    return [
        {
            'name': 'Sol Ring',
            'set': 'cmr',
            'lang': 'en',
            'games': ['paper', 'mtgo'],
            'oracle_id': 'abc-sol-ring',
            'border_color': 'black',
            'image_uris': {
                'small': 'https://img/cmr_small.jpg',
                'normal': 'https://img/cmr_normal.jpg',
                'large': 'https://img/cmr_large.jpg',
            },
            'prices': {'usd': '1.25', 'usd_foil': '4.50'},
        },
        {
            'name': 'Sol Ring',
            'set': 'lea',
            'lang': 'en',
            'games': ['paper'],
            'oracle_id': 'abc-sol-ring',
            'border_color': 'white',
            'image_uris': {'normal': 'https://img/lea_normal.jpg'},
            'prices': {'usd': None, 'usd_foil': None},
        },
        {
            # Borderless foil-only printing — price in usd_foil only.
            'name': 'Stoneforge Mystic',
            'set': 'sld',
            'lang': 'en',
            'games': ['paper'],
            'oracle_id': 'abc-stoneforge',
            'border_color': 'borderless',
            'image_uris': {'normal': 'https://img/sld_normal.jpg'},
            'prices': {'usd': None, 'usd_foil': '42.00'},
        },
        {
            # Transform card: image_uris live inside card_faces.
            'name': 'Delver of Secrets',
            'set': 'isd',
            'lang': 'en',
            'games': ['paper'],
            'oracle_id': 'abc-delver',
            'border_color': 'black',
            'card_faces': [
                {'image_uris': {'normal': 'https://img/delver_front.jpg'}},
                {'image_uris': {'normal': 'https://img/delver_back.jpg'}},
            ],
            'prices': {'usd': '0.50'},
        },
        {
            # Non-English printing — must be ignored.
            'name': 'Sol Ring',
            'set': 'jp',
            'lang': 'ja',
            'games': ['paper'],
            'oracle_id': 'abc-sol-ring',
            'border_color': 'black',
            'image_uris': {'normal': 'https://img/jp.jpg'},
            'prices': {'usd': '99.00'},
        },
    ]


class TestBuildCardOverlayInfo(unittest.TestCase):
    def setUp(self):
        # Import the helper fresh each test, with CARDS_DATA patched to
        # our fixture so we don't depend on the full Scryfall bulk cache.
        self._patcher = mock.patch('cards.CARDS_DATA', _fixture_cards())
        self._patcher.start()
        from web_server import _build_card_overlay_info
        self.build = _build_card_overlay_info

    def tearDown(self):
        self._patcher.stop()

    def test_missing_name_returns_none(self):
        self.assertIsNone(self.build(''))
        self.assertIsNone(self.build(None))

    def test_unknown_card_returns_none(self):
        self.assertIsNone(self.build('Black Lotus'))

    def test_returns_first_english_paper_printing_when_no_set(self):
        info = self.build('Sol Ring')
        self.assertIsNotNone(info)
        # fixture order: cmr comes before lea
        self.assertEqual(info['set'], 'cmr')
        self.assertEqual(info['border'], 'black')
        self.assertEqual(info['image_url'], 'https://img/cmr_normal.jpg')
        self.assertEqual(info['oracle_id'], 'abc-sol-ring')
        self.assertAlmostEqual(info['price_usd'], 1.25)

    def test_prefers_specific_set_when_provided(self):
        info = self.build('Sol Ring', 'lea')
        self.assertIsNotNone(info)
        self.assertEqual(info['set'], 'lea')
        self.assertEqual(info['border'], 'white')
        self.assertEqual(info['image_url'], 'https://img/lea_normal.jpg')
        # lea has no price in fixture — should gracefully report None.
        self.assertIsNone(info['price_usd'])

    def test_set_code_is_case_insensitive(self):
        info = self.build('sol ring', 'LEA')
        self.assertIsNotNone(info)
        self.assertEqual(info['set'], 'lea')

    def test_falls_back_to_usd_foil_when_usd_missing(self):
        info = self.build('Stoneforge Mystic')
        self.assertIsNotNone(info)
        self.assertEqual(info['border'], 'borderless')
        self.assertAlmostEqual(info['price_usd'], 42.00)

    def test_transform_card_uses_front_face_image(self):
        info = self.build('Delver of Secrets')
        self.assertIsNotNone(info)
        self.assertEqual(info['image_url'], 'https://img/delver_front.jpg')

    def test_ignores_non_english_printings(self):
        # Only way to hit the jp printing would be a set override.
        # With no override, we must still pick an English printing.
        info = self.build('Sol Ring')
        self.assertNotEqual(info['set'], 'jp')
        # Even an explicit set:jp request should miss (not English, so
        # no match at all — falls back to first English printing).
        info_jp = self.build('Sol Ring', 'jp')
        self.assertIsNotNone(info_jp)
        self.assertNotEqual(info_jp['set'], 'jp')

    def test_payload_has_required_keys(self):
        info = self.build('Sol Ring')
        for key in ('name', 'set', 'oracle_id',
                    'image_url', 'border', 'price_usd'):
            self.assertIn(key, info)


if __name__ == '__main__':
    unittest.main()
