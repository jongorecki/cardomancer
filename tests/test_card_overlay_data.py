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
    """Hand-crafted Scryfall-like card dicts covering the cases the overlay
    helper has to handle: multiple printings, missing price, a transform
    card with per-face image_uris, and two same-set same-name printings
    that differ only by collector number (the basic-Swamp case that
    motivated the ID-based lookup path)."""
    return [
        {
            'id': 'id-sol-cmr',
            'name': 'Sol Ring',
            'set': 'cmr',
            'collector_number': '472',
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
            'id': 'id-sol-lea',
            'name': 'Sol Ring',
            'set': 'lea',
            'collector_number': '270',
            'lang': 'en',
            'games': ['paper'],
            'oracle_id': 'abc-sol-ring',
            'border_color': 'white',
            'image_uris': {'normal': 'https://img/lea_normal.jpg'},
            'prices': {'usd': None, 'usd_foil': None},
        },
        {
            # Borderless foil-only printing — price in usd_foil only.
            'id': 'id-stoneforge-sld',
            'name': 'Stoneforge Mystic',
            'set': 'sld',
            'collector_number': '10',
            'lang': 'en',
            'games': ['paper'],
            'oracle_id': 'abc-stoneforge',
            'border_color': 'borderless',
            'image_uris': {'normal': 'https://img/sld_normal.jpg'},
            'prices': {'usd': None, 'usd_foil': '42.00'},
        },
        {
            # Transform card: image_uris live inside card_faces.
            'id': 'id-delver-isd',
            'name': 'Delver of Secrets',
            'set': 'isd',
            'collector_number': '51a',
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
            'id': 'id-sol-jp',
            'name': 'Sol Ring',
            'set': 'jp',
            'collector_number': '1',
            'lang': 'ja',
            'games': ['paper'],
            'oracle_id': 'abc-sol-ring',
            'border_color': 'black',
            'image_uris': {'normal': 'https://img/jp.jpg'},
            'prices': {'usd': '99.00'},
        },
        {
            # Same set + name, different collector numbers — the case that
            # exposed the printing-disambiguation bug for basic lands.
            'id': 'id-swamp-war-a',
            'name': 'Swamp',
            'set': 'war',
            'collector_number': '268',
            'lang': 'en',
            'games': ['paper'],
            'oracle_id': 'abc-swamp',
            'border_color': 'black',
            'image_uris': {'normal': 'https://img/swamp_war_a.jpg'},
            'prices': {'usd': '0.15'},
        },
        {
            'id': 'id-swamp-war-b',
            'name': 'Swamp',
            'set': 'war',
            'collector_number': '269',
            'lang': 'en',
            'games': ['paper'],
            'oracle_id': 'abc-swamp',
            'border_color': 'black',
            'image_uris': {'normal': 'https://img/swamp_war_b.jpg'},
            'prices': {'usd': '0.15'},
        },
    ]


def _fixture_by_id(cards_list):
    return {c['id']: c for c in cards_list}


class TestBuildCardOverlayInfo(unittest.TestCase):
    def setUp(self):
        # Import the helper fresh each test, with CARDS_DATA and
        # CARD_DATA_BY_ID patched to our fixture so we don't depend on
        # the full Scryfall bulk cache.
        fixture = _fixture_cards()
        self._patchers = [
            mock.patch('cards.CARDS_DATA', fixture),
            mock.patch('cards.CARD_DATA_BY_ID', _fixture_by_id(fixture)),
        ]
        for p in self._patchers:
            p.start()
        from web_server import _build_card_overlay_info
        self.build = _build_card_overlay_info

    def tearDown(self):
        for p in self._patchers:
            p.stop()

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
        for key in ('name', 'set', 'collector_number', 'oracle_id',
                    'scryfall_id', 'image_url', 'border', 'price_usd'):
            self.assertIn(key, info)

    def test_lookup_by_id_picks_exact_printing(self):
        # Name + set would be ambiguous — two Swamps in war with
        # different collector numbers. The ID disambiguates.
        info = self.build(None, None, card_id='id-swamp-war-b')
        self.assertIsNotNone(info)
        self.assertEqual(info['scryfall_id'], 'id-swamp-war-b')
        self.assertEqual(info['set'], 'war')
        self.assertEqual(info['collector_number'], '269')
        self.assertEqual(info['image_url'],
                         'https://img/swamp_war_b.jpg')

    def test_id_takes_precedence_over_name(self):
        # Even if name+set also resolve, the ID wins.
        info = self.build('Sol Ring', 'cmr', card_id='id-sol-lea')
        self.assertIsNotNone(info)
        self.assertEqual(info['set'], 'lea')
        self.assertEqual(info['scryfall_id'], 'id-sol-lea')

    def test_missing_id_and_name_returns_none(self):
        self.assertIsNone(self.build(None, None, card_id=None))
        self.assertIsNone(self.build('', None, card_id=''))

    def test_unknown_id_falls_back_to_name(self):
        info = self.build('Sol Ring', 'lea', card_id='id-does-not-exist')
        self.assertIsNotNone(info)
        self.assertEqual(info['set'], 'lea')


if __name__ == '__main__':
    unittest.main()
