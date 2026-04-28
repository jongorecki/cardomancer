# tests/test_drop_offset_persistence.py
# ---------------------------------------------------------------------------
# Verifies set_drop_offset() persists Z_DROP_OFFSET to drop_height.json
# and that _load_drop_offset_from_disk() reads it back. Also covers
# the validation that out-of-range values are rejected.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os

import pytest

import gcode_control


@pytest.fixture
def tmp_drop_height(tmp_path, monkeypatch):
    """Redirect drop_height.json to a tempdir and reset Z_DROP_OFFSET."""
    target = tmp_path / "drop_height.json"
    monkeypatch.setattr(gcode_control, "_DROP_HEIGHT_PATH", str(target))
    # Reset to a known starting value so each test starts clean.
    monkeypatch.setattr(gcode_control, "Z_DROP_OFFSET", 50.0)
    yield target


def test_set_drop_offset_persists_to_disk(tmp_drop_height):
    ok = gcode_control.set_drop_offset(35.0)
    assert ok is True
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(35.0)
    assert os.path.exists(tmp_drop_height)
    with open(tmp_drop_height, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["z_drop_offset"] == pytest.approx(35.0)
    assert "updated_at" in data


def test_set_drop_offset_rejects_too_low(tmp_drop_height):
    ok = gcode_control.set_drop_offset(2.0)  # below _DROP_OFFSET_MIN (5.0)
    assert ok is False
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(50.0)
    assert not os.path.exists(tmp_drop_height)


def test_set_drop_offset_rejects_too_high(tmp_drop_height):
    ok = gcode_control.set_drop_offset(200.0)  # above _DROP_OFFSET_MAX (150)
    assert ok is False
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(50.0)


def test_set_drop_offset_rejects_non_numeric(tmp_drop_height):
    ok = gcode_control.set_drop_offset("not a number")
    assert ok is False
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(50.0)


def test_load_drop_offset_from_disk_applies_value(tmp_drop_height, monkeypatch):
    # Pre-write a valid file, then call the loader.
    with open(tmp_drop_height, "w", encoding="utf-8") as fh:
        json.dump({"z_drop_offset": 42.5, "updated_at": "2026-04-27T00:00:00Z"},
                  fh)
    monkeypatch.setattr(gcode_control, "Z_DROP_OFFSET", 50.0)
    gcode_control._load_drop_offset_from_disk()
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(42.5)


def test_load_drop_offset_from_disk_ignores_corrupt_file(tmp_drop_height,
                                                          monkeypatch):
    # Write garbage that json.load can't parse — must NOT raise.
    with open(tmp_drop_height, "w", encoding="utf-8") as fh:
        fh.write("{this is not json")
    monkeypatch.setattr(gcode_control, "Z_DROP_OFFSET", 50.0)
    gcode_control._load_drop_offset_from_disk()  # should not raise
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(50.0)


def test_load_drop_offset_from_disk_ignores_out_of_range(tmp_drop_height,
                                                          monkeypatch):
    with open(tmp_drop_height, "w", encoding="utf-8") as fh:
        json.dump({"z_drop_offset": 999.0}, fh)
    monkeypatch.setattr(gcode_control, "Z_DROP_OFFSET", 50.0)
    gcode_control._load_drop_offset_from_disk()
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(50.0)


def test_load_drop_offset_from_disk_no_file(tmp_drop_height, monkeypatch):
    # File doesn't exist — loader should be a no-op, not raise.
    if os.path.exists(tmp_drop_height):
        os.remove(tmp_drop_height)
    monkeypatch.setattr(gcode_control, "Z_DROP_OFFSET", 50.0)
    gcode_control._load_drop_offset_from_disk()
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(50.0)


def test_set_drop_offset_round_trip(tmp_drop_height, monkeypatch):
    """Round-trip: save a value, reset memory, reload from disk."""
    gcode_control.set_drop_offset(67.25)
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(67.25)
    # Simulate a fresh Python process by resetting the global, then
    # calling the on-import loader.
    monkeypatch.setattr(gcode_control, "Z_DROP_OFFSET", 50.0)
    gcode_control._load_drop_offset_from_disk()
    assert gcode_control.Z_DROP_OFFSET == pytest.approx(67.25)
