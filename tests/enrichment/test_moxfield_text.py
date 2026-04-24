"""Tests for web_enrichment.moxfield_text — paste-based Moxfield import/export."""

from __future__ import annotations

from unittest import mock

import pytest

from web_enrichment import moxfield_text


# ---------------------------------------------------------------------------
# parse_text
# ---------------------------------------------------------------------------

def test_parse_text_basic_quantity_name():
    rows = moxfield_text.parse_text("4 Lightning Bolt\n1 Black Lotus")
    assert rows == [
        {"qty": 4, "name": "Lightning Bolt", "set": None, "collector_number": None},
        {"qty": 1, "name": "Black Lotus", "set": None, "collector_number": None},
    ]


def test_parse_text_with_set_and_collector_number():
    rows = moxfield_text.parse_text("4 Lightning Bolt (LEA) 161")
    assert rows == [
        {"qty": 4, "name": "Lightning Bolt", "set": "lea", "collector_number": "161"},
    ]


def test_parse_text_with_set_no_cn():
    rows = moxfield_text.parse_text("1 Black Lotus (LEA)")
    assert rows == [
        {"qty": 1, "name": "Black Lotus", "set": "lea", "collector_number": None},
    ]


def test_parse_text_quantity_with_x_suffix():
    rows = moxfield_text.parse_text("4x Lightning Bolt")
    assert rows[0]["qty"] == 4


def test_parse_text_strips_foil_prefix():
    rows = moxfield_text.parse_text("*F* 1 Black Lotus (LEA) 232")
    assert rows == [
        {"qty": 1, "name": "Black Lotus", "set": "lea", "collector_number": "232"},
    ]


def test_parse_text_strips_foil_suffix():
    rows = moxfield_text.parse_text("1 Black Lotus *F*")
    assert rows[0]["name"] == "Black Lotus"


def test_parse_text_ignores_comments_and_blank_lines():
    rows = moxfield_text.parse_text(
        "// this is a comment\n"
        "\n"
        "# hash comment\n"
        "4 Lightning Bolt\n"
        "\n"
    )
    assert len(rows) == 1
    assert rows[0]["name"] == "Lightning Bolt"


def test_parse_text_ignores_section_headers():
    rows = moxfield_text.parse_text(
        "Commander\n"
        "1 Atraxa, Praetors' Voice\n"
        "Sideboard\n"
        "2 Abrade\n"
    )
    assert [r["name"] for r in rows] == ["Atraxa, Praetors' Voice", "Abrade"]


def test_parse_text_ignores_zero_qty():
    # qty=0 is nonsense; drop it
    rows = moxfield_text.parse_text("0 Lightning Bolt\n1 Counterspell")
    assert [r["name"] for r in rows] == ["Counterspell"]


def test_parse_text_skips_unparseable_lines():
    rows = moxfield_text.parse_text(
        "this line has no quantity\n"
        "4 Lightning Bolt\n"
        "random words with no numbers\n"
    )
    assert len(rows) == 1


def test_parse_text_empty_input():
    assert moxfield_text.parse_text("") == []
    assert moxfield_text.parse_text(None) == []


def test_parse_text_handles_multiword_names():
    rows = moxfield_text.parse_text("1 Jace, the Mind Sculptor (WWK) 31")
    assert rows == [
        {"qty": 1, "name": "Jace, the Mind Sculptor", "set": "wwk",
         "collector_number": "31"},
    ]


# ---------------------------------------------------------------------------
# resolve_entries
# ---------------------------------------------------------------------------

_FAKE_CARDS = [
    {"name": "Lightning Bolt", "oracle_id": "oid-lb", "set": "lea",
     "collector_number": "161", "lang": "en", "games": ["paper"],
     "image_uris": {"small": "https://img/lb-lea-small.jpg"}},
    {"name": "Lightning Bolt", "oracle_id": "oid-lb", "set": "m11",
     "collector_number": "149", "lang": "en", "games": ["paper"],
     "image_uris": {"small": "https://img/lb-m11-small.jpg"}},
    {"name": "Black Lotus", "oracle_id": "oid-bl", "set": "lea",
     "collector_number": "232", "lang": "en", "games": ["paper"],
     "image_uris": {"small": "https://img/bl-small.jpg"}},
    # Non-English / non-paper should be ignored.
    {"name": "Counterspell", "oracle_id": "oid-cs", "set": "lea",
     "lang": "ja", "games": ["paper"]},
    {"name": "Counterspell", "oracle_id": "oid-cs", "set": "lea",
     "lang": "en", "games": ["arena"]},
]


def _patch_cards(monkeypatch, cards=None):
    monkeypatch.setattr("cards.CARDS_DATA", cards or _FAKE_CARDS, raising=False)


def test_resolve_entries_matches_by_name(monkeypatch):
    _patch_cards(monkeypatch)
    entries = [{"qty": 4, "name": "Lightning Bolt",
                "set": None, "collector_number": None}]
    resolved, unresolved = moxfield_text.resolve_entries(entries)
    assert unresolved == []
    assert len(resolved) == 1
    assert resolved[0]["oracle_id"] == "oid-lb"
    assert resolved[0]["name"] == "Lightning Bolt"


def test_resolve_entries_prefers_matching_set(monkeypatch):
    _patch_cards(monkeypatch)
    entries = [{"qty": 1, "name": "Lightning Bolt",
                "set": "m11", "collector_number": None}]
    resolved, unresolved = moxfield_text.resolve_entries(entries)
    assert resolved[0]["set_code"] == "m11"


def test_resolve_entries_prefers_matching_set_and_cn(monkeypatch):
    _patch_cards(monkeypatch)
    entries = [{"qty": 1, "name": "Lightning Bolt",
                "set": "lea", "collector_number": "161"}]
    resolved, _ = moxfield_text.resolve_entries(entries)
    assert resolved[0]["set_code"] == "lea"


def test_resolve_entries_unknown_name(monkeypatch):
    _patch_cards(monkeypatch)
    entries = [{"qty": 1, "name": "Nonexistent Card",
                "set": None, "collector_number": None}]
    resolved, unresolved = moxfield_text.resolve_entries(entries)
    assert resolved == []
    assert unresolved == ["Nonexistent Card"]


def test_resolve_entries_case_insensitive(monkeypatch):
    _patch_cards(monkeypatch)
    entries = [{"qty": 1, "name": "lIgHtNiNg BoLt",
                "set": None, "collector_number": None}]
    resolved, unresolved = moxfield_text.resolve_entries(entries)
    assert unresolved == []
    assert resolved[0]["oracle_id"] == "oid-lb"


def test_resolve_entries_dedups_on_oracle_id(monkeypatch):
    _patch_cards(monkeypatch)
    entries = [
        {"qty": 4, "name": "Lightning Bolt", "set": None, "collector_number": None},
        {"qty": 2, "name": "Lightning Bolt", "set": "m11", "collector_number": None},
    ]
    resolved, _ = moxfield_text.resolve_entries(entries)
    # Same oracle_id, one entry survives (second overwrites first).
    assert len(resolved) == 1


def test_resolve_entries_ignores_non_english_and_non_paper(monkeypatch):
    _patch_cards(monkeypatch)
    entries = [{"qty": 1, "name": "Counterspell",
                "set": None, "collector_number": None}]
    resolved, unresolved = moxfield_text.resolve_entries(entries)
    # Both Counterspell printings in fixture are either ja or arena.
    assert resolved == []
    assert unresolved == ["Counterspell"]


def test_resolve_entries_falls_back_on_set_mismatch(monkeypatch):
    _patch_cards(monkeypatch)
    entries = [{"qty": 1, "name": "Lightning Bolt",
                "set": "zzz", "collector_number": None}]
    resolved, unresolved = moxfield_text.resolve_entries(entries)
    # Set didn't match anywhere, fall back to first candidate.
    assert unresolved == []
    assert resolved[0]["oracle_id"] == "oid-lb"


# ---------------------------------------------------------------------------
# render_text
# ---------------------------------------------------------------------------

def test_render_text_basic():
    rows = [
        {"name": "Lightning Bolt", "set_code": "lea",
         "collector_number": "161", "quantity": 4, "foil_quantity": 0},
    ]
    assert moxfield_text.render_text(rows) == "4 Lightning Bolt (LEA) 161"


def test_render_text_splits_foil():
    rows = [
        {"name": "Black Lotus", "set_code": "lea",
         "collector_number": "232", "quantity": 1, "foil_quantity": 1},
    ]
    out = moxfield_text.render_text(rows).splitlines()
    assert "1 Black Lotus (LEA) 232" in out
    assert "*F* 1 Black Lotus (LEA) 232" in out


def test_render_text_no_set_no_cn():
    rows = [{"name": "Lightning Bolt", "quantity": 4}]
    assert moxfield_text.render_text(rows) == "4 Lightning Bolt"


def test_render_text_skips_zero_qty():
    rows = [
        {"name": "Lightning Bolt", "set_code": "lea", "quantity": 0, "foil_quantity": 0},
        {"name": "Black Lotus", "quantity": 1},
    ]
    assert moxfield_text.render_text(rows) == "1 Black Lotus"


def test_render_text_skips_empty_name():
    rows = [{"name": "", "quantity": 4}, {"name": "Lightning Bolt", "quantity": 1}]
    assert moxfield_text.render_text(rows) == "1 Lightning Bolt"


def test_render_text_empty_input():
    assert moxfield_text.render_text([]) == ""


def test_round_trip_parse_then_render(monkeypatch):
    _patch_cards(monkeypatch)
    text = "4 Lightning Bolt (LEA) 161\n1 Black Lotus (LEA) 232"
    entries = moxfield_text.parse_text(text)
    # Bolt the "quantity" onto the rendered rows so it round-trips.
    rows = [
        {"name": e["name"], "set_code": e["set"],
         "collector_number": e["collector_number"],
         "quantity": e["qty"], "foil_quantity": 0}
        for e in entries
    ]
    assert moxfield_text.render_text(rows) == text
