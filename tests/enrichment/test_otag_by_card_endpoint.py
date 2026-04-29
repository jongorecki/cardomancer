# tests/enrichment/test_otag_by_card_endpoint.py
# ---------------------------------------------------------------------------
# Tests for the by-card lookup helper. Validates ancestor-walk behavior,
# longest-chain-wins for multi-rooted DAGs, and graceful no-otag handling.
# ---------------------------------------------------------------------------

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from web_enrichment.otag_explorer import by_card


def _seed_tags(conn, tag_oracle_pairs):
    rows = [(oid, t, 'test') for t, oid in tag_oracle_pairs]
    conn.executemany(
        "INSERT OR IGNORE INTO tags (oracle_id, tag_name, source) VALUES (?,?,?)",
        rows,
    )
    conn.commit()


def _seed_hierarchy(conn, edges):
    """edges: [(child, parent)]"""
    rows = [(c, p, 'hierarchy', 1.0, 'test', '2026-01-01') for c, p in edges]
    conn.executemany(
        """INSERT OR REPLACE INTO otag_relations
           (src_otag, dst_otag, relation_type, weight, source, last_updated)
           VALUES (?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def _seed_catalog(conn, names):
    rows = [(n, 'function', None, None, 100, 'test') for n in names]
    conn.executemany(
        """INSERT OR REPLACE INTO tag_catalog
           (tag_name, tag_type, parent, description,
            card_count_expected, source)
           VALUES (?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()


def _card(name='Lightning Bolt', oracle_id='oid-bolt', cid='sf-bolt'):
    return {
        'id': cid, 'name': name, 'oracle_id': oracle_id,
        'lang': 'en', 'games': ['paper'],
        'set': 'm11', 'collector_number': '147',
        'image_uris': {'normal': 'https://example/bolt.png'},
    }


def test_unresolvable_card_returns_none(tmp_enrichment_db):
    res = by_card(tmp_enrichment_db, [], {}, scryfall_id='no-such')
    assert res is None


def test_card_with_no_otags(tmp_enrichment_db):
    c = _card()
    res = by_card(tmp_enrichment_db, [c], {c['id']: c}, scryfall_id=c['id'])
    assert res is not None
    assert res['otags'] == []
    assert res['card']['name'] == 'Lightning Bolt'


def test_simple_ancestor_walk(tmp_enrichment_db):
    c = _card()
    _seed_tags(tmp_enrichment_db, [('removal', 'oid-bolt')])
    _seed_catalog(tmp_enrichment_db, ['removal', 'creature-removal', 'interaction'])
    # removal -> creature-removal -> interaction
    _seed_hierarchy(tmp_enrichment_db, [
        ('removal', 'creature-removal'),
        ('creature-removal', 'interaction'),
    ])
    res = by_card(tmp_enrichment_db, [c], {c['id']: c}, scryfall_id=c['id'])
    assert len(res['otags']) == 1
    o = res['otags'][0]
    assert o['otag'] == 'removal'
    assert o['ancestors'] == ['creature-removal', 'interaction']


def test_longest_chain_wins_in_multi_parent_dag(tmp_enrichment_db):
    c = _card()
    _seed_tags(tmp_enrichment_db, [('ramp', 'oid-bolt')])
    _seed_catalog(tmp_enrichment_db, [
        'ramp', 'land-ramp', 'mana-acceleration', 'fast-mana', 'cheating'
    ])
    # Two paths upward:
    #   ramp -> land-ramp -> mana-acceleration  (length 2)
    #   ramp -> fast-mana -> cheating -> mana-acceleration (length 3)
    _seed_hierarchy(tmp_enrichment_db, [
        ('ramp', 'land-ramp'),
        ('land-ramp', 'mana-acceleration'),
        ('ramp', 'fast-mana'),
        ('fast-mana', 'cheating'),
        ('cheating', 'mana-acceleration'),
    ])
    res = by_card(tmp_enrichment_db, [c], {c['id']: c}, scryfall_id=c['id'])
    o = res['otags'][0]
    # Longest chain wins
    assert o['ancestors'] == ['fast-mana', 'cheating', 'mana-acceleration']


def test_lookup_by_name_set_cn(tmp_enrichment_db):
    c = _card()
    other = _card(name='Other', cid='sf-other', oracle_id='oid-other')
    cards = [c, other]
    by_id = {c['id']: c, other['id']: other}
    res = by_card(tmp_enrichment_db, cards, by_id,
                  name='Lightning Bolt', set_code='M11', cn='147')
    assert res is not None
    assert res['card']['scryfall_id'] == c['id']


def test_cycle_in_hierarchy_terminates(tmp_enrichment_db):
    c = _card()
    _seed_tags(tmp_enrichment_db, [('a', 'oid-bolt')])
    _seed_catalog(tmp_enrichment_db, ['a', 'b'])
    # a -> b -> a (cycle); walker must not loop forever
    _seed_hierarchy(tmp_enrichment_db, [('a', 'b'), ('b', 'a')])
    res = by_card(tmp_enrichment_db, [c], {c['id']: c}, scryfall_id=c['id'])
    o = res['otags'][0]
    # The walk should terminate; the reported ancestor list contains 'b'
    # but not loop back into 'a'.
    assert 'b' in o['ancestors']
    assert 'a' not in o['ancestors']
