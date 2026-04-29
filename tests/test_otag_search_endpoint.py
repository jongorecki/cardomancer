# tests/test_otag_search_endpoint.py
# ---------------------------------------------------------------------------
# Tests for the search helper powering /api/otags/search.
#
# These exercise the pure helper directly with synthetic CARDS_DATA + a
# tmp enrichment.db so we can assert ranking without hitting Flask.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_enrichment.otag_explorer import search_cards_and_otags


def _seed_catalog(conn, rows):
    """rows: [(tag_name, card_count_expected)]"""
    conn.executemany(
        """INSERT OR REPLACE INTO tag_catalog
           (tag_name, tag_type, parent, description,
            card_count_expected, source)
           VALUES (?, 'function', NULL, NULL, ?, 'test')""",
        rows,
    )
    conn.commit()


def _card(name, edhrec_rank=None, set_code='m11', cn='1', cid=None):
    return {
        'name': name,
        'lang': 'en',
        'games': ['paper'],
        'set': set_code,
        'collector_number': cn,
        'edhrec_rank': edhrec_rank,
        'id': cid or name.lower().replace(' ', '-'),
    }


def test_card_prefix_ranks_above_substring(tmp_enrichment_db):
    cards = [
        _card('Lightning Helix', edhrec_rank=200),
        _card('Lightning Bolt',  edhrec_rank=10),
        _card('Volcanic Lightning', edhrec_rank=50),
        _card('Bolt Hound', edhrec_rank=400),
    ]
    _seed_catalog(tmp_enrichment_db, [])
    out = search_cards_and_otags(cards, tmp_enrichment_db, 'lightning', limit=10)
    cards_only = [r for r in out if r['kind'] == 'card']
    # Lightning Bolt and Lightning Helix are prefix matches; both should
    # come before "Volcanic Lightning" (substring).
    names = [r['label'] for r in cards_only]
    assert names.index('Lightning Bolt') < names.index('Volcanic Lightning')
    assert names.index('Lightning Helix') < names.index('Volcanic Lightning')


def test_card_edhrec_rank_secondary_sort(tmp_enrichment_db):
    cards = [
        _card('Lightning Helix', edhrec_rank=200),
        _card('Lightning Bolt',  edhrec_rank=10),
        _card('Lightning Strike', edhrec_rank=None),
    ]
    out = search_cards_and_otags(cards, tmp_enrichment_db, 'lightning', limit=10)
    cards_only = [r for r in out if r['kind'] == 'card']
    names = [r['label'] for r in cards_only]
    assert names[0] == 'Lightning Bolt'
    assert names[1] == 'Lightning Helix'
    # NULL rank goes last
    assert names[-1] == 'Lightning Strike'


def test_otag_card_count_desc(tmp_enrichment_db):
    _seed_catalog(tmp_enrichment_db, [
        ('ramp', 2113),
        ('ramping ritual', 50),
        ('ramp-pwa', None),
    ])
    out = search_cards_and_otags([], tmp_enrichment_db, 'ramp', limit=10)
    otags = [r for r in out if r['kind'] == 'otag']
    labels = [r['label'] for r in otags]
    assert labels[0] == 'ramp'
    assert labels[1] == 'ramping ritual'
    # NULL last
    assert labels[-1] == 'ramp-pwa'


def test_filters_non_paper_and_non_english(tmp_enrichment_db):
    cards = [
        _card('Lightning Bolt', edhrec_rank=10),
        # mtgo only — must be filtered out
        {'name': 'Lightning Bolt MTGO', 'lang': 'en', 'games': ['mtgo'],
         'edhrec_rank': 5, 'id': 'lbm'},
        # japanese — must be filtered out
        {'name': 'Lightning Bolt JP', 'lang': 'ja', 'games': ['paper'],
         'edhrec_rank': 5, 'id': 'lbj'},
    ]
    out = search_cards_and_otags(cards, tmp_enrichment_db, 'lightning', limit=10)
    labels = [r['label'] for r in out if r['kind'] == 'card']
    assert labels == ['Lightning Bolt']


def test_combined_limit_split_evenly(tmp_enrichment_db):
    cards = [_card(f'Lightning Card {i}', edhrec_rank=i)
             for i in range(20)]
    _seed_catalog(tmp_enrichment_db, [
        (f'lightning-tag-{i}', 100 - i) for i in range(20)
    ])
    out = search_cards_and_otags(cards, tmp_enrichment_db, 'lightning', limit=10)
    n_cards = sum(1 for r in out if r['kind'] == 'card')
    n_otags = sum(1 for r in out if r['kind'] == 'otag')
    assert n_cards <= 5 and n_otags <= 5
    assert len(out) == 10
