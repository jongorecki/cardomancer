# tests/test_detection_reviews.py
# ---------------------------------------------------------------------------
# Per-attribute detection review queues (Phase 4 item 4.20).
#
# Covers:
#   - inserting a review row
#   - listing a queue sorted by ascending confidence (NULLs first)
#   - filtering by variable
#   - marking a verdict (correct / wrong / skip)
#   - idempotency — re-submitting the same verdict, re-seeding
# ---------------------------------------------------------------------------

from __future__ import annotations

import pytest

import collection_db


def _make_scan(conn, session_id, scan_num, *, recognized=1,
               name='Island', set_code='lea', collector_number='286'):
    """Insert a minimal scan_history row and return its id."""
    cur = conn.execute(
        """INSERT INTO scan_history
           (session_id, scan_num, timestamp, name, set_code, collector_number,
            recognized)
           VALUES (?, ?, '2026-04-23T00:00:00', ?, ?, ?, ?)""",
        (session_id, scan_num, name, set_code, collector_number, recognized)
    )
    conn.commit()
    return cur.lastrowid


def _make_session(conn):
    cur = conn.execute(
        "INSERT INTO sessions (start_time) VALUES ('2026-04-23T00:00:00')"
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Insert
# ---------------------------------------------------------------------------

def test_upsert_creates_row(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)

    rid = collection_db.upsert_detection_review(
        conn, scan_id, 'foil',
        detected_value='nonfoil', confidence=0.82)
    assert rid is not None

    row = conn.execute(
        "SELECT * FROM detection_reviews WHERE id=?", (rid,)
    ).fetchone()
    assert row['variable'] == 'foil'
    assert row['detected_value'] == 'nonfoil'
    assert row['confidence'] == pytest.approx(0.82)
    assert row['verdict'] is None


def test_upsert_rejects_unknown_variable(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)
    with pytest.raises(ValueError):
        collection_db.upsert_detection_review(conn, scan_id, 'not_a_variable')


def test_upsert_is_idempotent_when_no_verdict(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)

    rid1 = collection_db.upsert_detection_review(
        conn, scan_id, 'foil', detected_value='nonfoil', confidence=0.5)
    rid2 = collection_db.upsert_detection_review(
        conn, scan_id, 'foil', detected_value='foil', confidence=0.9)
    assert rid1 == rid2

    row = conn.execute(
        "SELECT * FROM detection_reviews WHERE id=?", (rid1,)
    ).fetchone()
    # Refreshed to the latest detector output.
    assert row['detected_value'] == 'foil'
    assert row['confidence'] == pytest.approx(0.9)

    count = conn.execute(
        "SELECT COUNT(*) FROM detection_reviews").fetchone()[0]
    assert count == 1


def test_upsert_preserves_verdict(tmp_collection_db):
    """Once a user has judged the item, re-seeding must not stomp the verdict."""
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)

    rid = collection_db.upsert_detection_review(
        conn, scan_id, 'foil', detected_value='nonfoil', confidence=0.5)
    collection_db.set_detection_verdict(conn, rid, 'correct')

    # Re-emit (e.g. re-run detector) — verdict must remain, detected_value
    # must not be overwritten after review.
    collection_db.upsert_detection_review(
        conn, scan_id, 'foil', detected_value='foil', confidence=0.99)

    row = conn.execute(
        "SELECT * FROM detection_reviews WHERE id=?", (rid,)
    ).fetchone()
    assert row['verdict'] == 'correct'
    assert row['detected_value'] == 'nonfoil'  # frozen at review time
    assert row['confidence'] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# List / filter / sort
# ---------------------------------------------------------------------------

def test_list_sorted_ascending_nulls_first(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    s1 = _make_scan(conn, session_id, 1, name='Card A')
    s2 = _make_scan(conn, session_id, 2, name='Card B')
    s3 = _make_scan(conn, session_id, 3, name='Card C')
    s4 = _make_scan(conn, session_id, 4, name='Card D')

    collection_db.upsert_detection_review(
        conn, s1, 'foil', confidence=0.9)
    collection_db.upsert_detection_review(
        conn, s2, 'foil', confidence=0.1)
    collection_db.upsert_detection_review(
        conn, s3, 'foil', confidence=None)  # detector didn't score it
    collection_db.upsert_detection_review(
        conn, s4, 'foil', confidence=0.5)

    items = collection_db.list_detection_reviews(conn, 'foil')
    # NULL must come first, then ascending numeric confidence.
    confidences = [i['confidence'] for i in items]
    assert confidences == [None, pytest.approx(0.1), pytest.approx(0.5),
                           pytest.approx(0.9)]


def test_list_filters_by_variable(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)

    collection_db.upsert_detection_review(conn, scan_id, 'foil', confidence=0.1)
    collection_db.upsert_detection_review(conn, scan_id, 'border', confidence=0.2)
    collection_db.upsert_detection_review(conn, scan_id, 'set_symbol',
                                          detected_value='lea', confidence=0.3)

    foil = collection_db.list_detection_reviews(conn, 'foil')
    border = collection_db.list_detection_reviews(conn, 'border')
    setsym = collection_db.list_detection_reviews(conn, 'set_symbol')

    assert [r['variable'] for r in foil] == ['foil']
    assert [r['variable'] for r in border] == ['border']
    assert [r['variable'] for r in setsym] == ['set_symbol']


def test_list_rejects_unknown_variable(tmp_collection_db):
    with pytest.raises(ValueError):
        collection_db.list_detection_reviews(tmp_collection_db, 'nope')


def test_list_hides_reviewed_by_default(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    s1 = _make_scan(conn, session_id, 1)
    s2 = _make_scan(conn, session_id, 2)

    r1 = collection_db.upsert_detection_review(conn, s1, 'foil', confidence=0.1)
    collection_db.upsert_detection_review(conn, s2, 'foil', confidence=0.2)
    collection_db.set_detection_verdict(conn, r1, 'correct')

    pending = collection_db.list_detection_reviews(conn, 'foil')
    assert len(pending) == 1

    everything = collection_db.list_detection_reviews(
        conn, 'foil', include_reviewed=True)
    assert len(everything) == 2


# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

def test_mark_verdict_correct(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)
    rid = collection_db.upsert_detection_review(
        conn, scan_id, 'foil', confidence=0.4)

    assert collection_db.set_detection_verdict(conn, rid, 'correct') is True

    row = conn.execute(
        "SELECT * FROM detection_reviews WHERE id=?", (rid,)
    ).fetchone()
    assert row['verdict'] == 'correct'
    assert row['reviewed_at'] is not None


def test_mark_verdict_wrong_with_correction(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)
    rid = collection_db.upsert_detection_review(
        conn, scan_id, 'border', detected_value='bordered', confidence=0.4)

    collection_db.set_detection_verdict(
        conn, rid, 'wrong', correction='borderless')

    row = conn.execute(
        "SELECT * FROM detection_reviews WHERE id=?", (rid,)
    ).fetchone()
    assert row['verdict'] == 'wrong'
    assert row['correction'] == 'borderless'


def test_mark_verdict_rejects_bad_value(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)
    rid = collection_db.upsert_detection_review(
        conn, scan_id, 'foil', confidence=0.4)

    with pytest.raises(ValueError):
        collection_db.set_detection_verdict(conn, rid, 'maybe')


def test_mark_verdict_missing_row_returns_false(tmp_collection_db):
    assert collection_db.set_detection_verdict(
        tmp_collection_db, 99999, 'correct') is False


def test_mark_verdict_idempotent(tmp_collection_db):
    """Re-submitting the same verdict must succeed and just refresh the timestamp."""
    conn = tmp_collection_db
    session_id = _make_session(conn)
    scan_id = _make_scan(conn, session_id, 1)
    rid = collection_db.upsert_detection_review(
        conn, scan_id, 'foil', confidence=0.4)

    collection_db.set_detection_verdict(conn, rid, 'correct')
    first = conn.execute(
        "SELECT verdict, reviewed_at FROM detection_reviews WHERE id=?",
        (rid,)).fetchone()

    collection_db.set_detection_verdict(conn, rid, 'correct')
    second = conn.execute(
        "SELECT verdict, reviewed_at FROM detection_reviews WHERE id=?",
        (rid,)).fetchone()

    # Verdict unchanged, still exactly one row for that (scan, variable).
    assert first['verdict'] == second['verdict'] == 'correct'
    count = conn.execute(
        "SELECT COUNT(*) FROM detection_reviews").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# Seeding from scan_history
# ---------------------------------------------------------------------------

def test_seed_creates_one_row_per_variable_per_scan(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    _make_scan(conn, session_id, 1, name='A', set_code='lea')
    _make_scan(conn, session_id, 2, name='B', set_code='m19')

    inserted = collection_db.seed_detection_reviews_from_scans(conn)
    # 2 scans * 3 variables
    assert inserted == 6
    total = conn.execute(
        "SELECT COUNT(*) FROM detection_reviews").fetchone()[0]
    assert total == 6

    # set_symbol review should have detected_value = set_code.
    sym = conn.execute(
        "SELECT detected_value FROM detection_reviews "
        "WHERE variable='set_symbol' ORDER BY scan_id").fetchall()
    assert [r['detected_value'] for r in sym] == ['lea', 'm19']

    # foil + border have no detector output → detected_value stays NULL.
    unknown = conn.execute(
        "SELECT COUNT(*) FROM detection_reviews "
        "WHERE variable IN ('foil','border') AND detected_value IS NULL"
    ).fetchone()[0]
    assert unknown == 4


def test_seed_is_idempotent(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    _make_scan(conn, session_id, 1)

    first = collection_db.seed_detection_reviews_from_scans(conn)
    second = collection_db.seed_detection_reviews_from_scans(conn)
    assert first == 3  # foil, border, set_symbol
    assert second == 0  # nothing new

    total = conn.execute(
        "SELECT COUNT(*) FROM detection_reviews").fetchone()[0]
    assert total == 3


def test_seed_skips_unrecognized_by_default(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    _make_scan(conn, session_id, 1, recognized=1)
    _make_scan(conn, session_id, 2, recognized=0)

    inserted = collection_db.seed_detection_reviews_from_scans(conn)
    assert inserted == 3  # only the recognized scan


def test_counts(tmp_collection_db):
    conn = tmp_collection_db
    session_id = _make_session(conn)
    s1 = _make_scan(conn, session_id, 1)
    s2 = _make_scan(conn, session_id, 2)

    r1 = collection_db.upsert_detection_review(conn, s1, 'foil', confidence=0.1)
    collection_db.upsert_detection_review(conn, s2, 'foil', confidence=0.2)
    collection_db.upsert_detection_review(conn, s1, 'border', confidence=0.3)
    collection_db.set_detection_verdict(conn, r1, 'correct')

    counts = collection_db.get_detection_review_counts(conn)
    assert counts['foil'] == {'pending': 1, 'reviewed': 1}
    assert counts['border'] == {'pending': 1, 'reviewed': 0}
    assert counts['set_symbol'] == {'pending': 0, 'reviewed': 0}
