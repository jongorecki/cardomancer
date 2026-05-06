"""Unit tests for the Z-bounce retry feature.

Covers:
  1. `pick_from_position(X_SOURCE_BIN)` with no `bounce` kwarg emits the
     same G-code sequence as before — no Z moves between `M106 P0 S255`
     and the final lift.
  2. `pick_from_position(X_SOURCE_BIN, bounce=True)` emits exactly
     `Z_BOUNCE_COUNT` down/up pairs between vacuum-on and the final
     lift, every Z move followed by `M400`, no X moves in that block.
  3. The worker call-site logic: when `_no_detect_retries == 0` the
     `bounce` kwarg is False; when `_no_detect_retries > 0` it's True.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gcode_control  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_gcode(monkeypatch):
    """Patch `_send_and_wait` to append every issued G-code line to a list.

    Also patch `_probe_with_cache` to NOT hit the serial — instead, just
    seed `_probe_z_cache[x_position]` with a known contact Z of 100.0
    and do not emit any commands (mirrors what a real probe would leave
    in the cache without the physical probe sequence).
    """
    issued = []

    def fake_send_and_wait(cmd):
        issued.append(cmd)

    def fake_probe_with_cache(x_position):
        gcode_control._probe_z_cache[x_position] = 100.0

    monkeypatch.setattr(gcode_control, "_send_and_wait", fake_send_and_wait)
    monkeypatch.setattr(
        gcode_control, "_probe_with_cache", fake_probe_with_cache
    )

    # Ensure a clean cache across tests.
    gcode_control._probe_z_cache.clear()

    return issued


def _slice_between(lines, start_marker, end_marker_prefix):
    """Return the sublist of `lines` strictly between `start_marker`
    and the first line starting with `end_marker_prefix` after it."""
    start = lines.index(start_marker)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith(end_marker_prefix):
            return lines[start + 1:i], i
    raise AssertionError(
        f"No line starting with {end_marker_prefix!r} found after "
        f"{start_marker!r} in {lines!r}"
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_no_bounce_default(captured_gcode):
    """With no `bounce` kwarg, the command stream between vacuum-on and
    the final Z lift contains only the vacuum dwell — no Z moves, no X
    moves, no extra syncs. Regression guard for unrelated callers that
    rely on the default behaviour.
    """
    gcode_control.pick_from_position(gcode_control.X_SOURCE_BIN)

    # The final lift is `G0 Z{z_clear} F{Z_FEEDRATE}`; find the LAST
    # `G0 Z...` line — that's the lift — and inspect the window between
    # vacuum-on and it.
    vacuum_idx = captured_gcode.index("M106 P0 S255")
    # Last G0 Z line is the final lift.
    lift_idx = max(
        i for i, c in enumerate(captured_gcode) if c.startswith("G0 Z")
    )
    assert lift_idx > vacuum_idx, "final lift must come after vacuum-on"

    between = captured_gcode[vacuum_idx + 1:lift_idx]

    # The only thing between vacuum-on and the final lift should be the
    # `G4 P{VACUUM_ON_DELAY_MS}` dwell.
    expected = [f"G4 P{gcode_control.VACUUM_ON_DELAY_MS}"]
    assert between == expected, (
        f"No-bounce path should not inject any Z or X moves between "
        f"vacuum-on and final lift. Got: {between!r}"
    )


def test_bounce_sequence(captured_gcode):
    """`bounce=True` inserts Z_BOUNCE_COUNT up/down pairs between
    vacuum-on and the final lift. Every Z move is followed by `M400`.
    No X moves appear anywhere in that block.
    """
    gcode_control.pick_from_position(
        gcode_control.X_SOURCE_BIN, bounce=True
    )

    vacuum_idx = captured_gcode.index("M106 P0 S255")
    # The final lift is the LAST G0 Z line (the bounce emits earlier G0 Z
    # lines, so we cannot just take the first one after vacuum-on).
    lift_idx = max(
        i for i, c in enumerate(captured_gcode) if c.startswith("G0 Z")
    )
    assert lift_idx > vacuum_idx, "final lift must come after vacuum-on"

    block = captured_gcode[vacuum_idx + 1:lift_idx]

    # The dwell is the first line of this block.
    assert block[0] == f"G4 P{gcode_control.VACUUM_ON_DELAY_MS}", (
        f"Expected vacuum dwell as first line after M106; got {block[0]!r}"
    )
    bounce_block = block[1:]

    # No X moves anywhere in the bounce block.
    for line in bounce_block:
        assert not line.startswith("G0 X"), (
            f"No X moves allowed between vacuum-on and final lift; "
            f"found: {line!r}"
        )
        assert not line.startswith("G1 X"), (
            f"No X moves allowed between vacuum-on and final lift; "
            f"found: {line!r}"
        )

    # Expected: Z_BOUNCE_COUNT pairs of (G0 Z up, M400, G0 Z down, M400).
    expected_len = gcode_control.Z_BOUNCE_COUNT * 4
    assert len(bounce_block) == expected_len, (
        f"Expected {expected_len} bounce lines "
        f"(Z_BOUNCE_COUNT={gcode_control.Z_BOUNCE_COUNT} * 4), "
        f"got {len(bounce_block)}: {bounce_block!r}"
    )

    contact_z = 100.0
    up_z = contact_z + gcode_control.Z_BOUNCE_DISTANCE_MM
    feed = gcode_control.Z_BOUNCE_FEEDRATE

    down_up_pairs = 0
    for i in range(gcode_control.Z_BOUNCE_COUNT):
        up = bounce_block[i * 4]
        up_sync = bounce_block[i * 4 + 1]
        down = bounce_block[i * 4 + 2]
        down_sync = bounce_block[i * 4 + 3]
        assert up == f"G0 Z{up_z} F{feed}", (
            f"Bounce pulse {i}: expected up move G0 Z{up_z} F{feed}, "
            f"got {up!r}"
        )
        assert up_sync == "M400", (
            f"Bounce pulse {i}: Z up must be followed by M400, got {up_sync!r}"
        )
        assert down == f"G0 Z{contact_z} F{feed}", (
            f"Bounce pulse {i}: expected down move G0 Z{contact_z} F{feed}, "
            f"got {down!r}"
        )
        assert down_sync == "M400", (
            f"Bounce pulse {i}: Z down must be followed by M400, "
            f"got {down_sync!r}"
        )
        down_up_pairs += 1

    assert down_up_pairs == gcode_control.Z_BOUNCE_COUNT


def test_bounce_flag_from_no_detect_retries(monkeypatch):
    """Mirror the call-site logic at web_worker.py:1052 — the `bounce`
    kwarg must be False iff `_no_detect_retries == 0` and True once
    retries have been incremented.

    Replicates the exact expression used in `_cmd_detect_and_sort` so
    a future refactor that changes the predicate trips this test.
    """
    # Import lazily so other tests aren't slowed down by the web_worker
    # import chain (pulls in cv2 etc).
    sys.path.insert(
        0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )

    recorded_calls = []

    def fake_pick(x_position, bounce=False):
        recorded_calls.append({"x": x_position, "bounce": bounce})

    monkeypatch.setattr(gcode_control, "pick_from_position", fake_pick)

    # We don't construct a full SortWorker — its __init__ doesn't need
    # extra args, but importing web_worker pulls in cv2 and web_motion_sim
    # which we avoid unless necessary. The call-site logic is trivial:
    #   bounce = self._no_detect_retries > 0
    # Exercise it twice: first call with 0 retries, then with 1.
    class _Stub:
        _no_detect_retries = 0

        @staticmethod
        def _call():
            gcode_control.pick_from_position(
                gcode_control.X_SOURCE_BIN,
                bounce=_Stub._no_detect_retries > 0,
            )

    _Stub._no_detect_retries = 0
    _Stub._call()
    assert recorded_calls[0] == {
        "x": gcode_control.X_SOURCE_BIN,
        "bounce": False,
    }, f"First pickup (no retries) must not bounce. Got {recorded_calls[0]!r}"

    _Stub._no_detect_retries = 1
    _Stub._call()
    assert recorded_calls[1] == {
        "x": gcode_control.X_SOURCE_BIN,
        "bounce": True,
    }, f"Retry pickup (1 miss) must bounce. Got {recorded_calls[1]!r}"

    _Stub._no_detect_retries = 2
    _Stub._call()
    assert recorded_calls[2]["bounce"] is True


def test_worker_call_site_matches_expression():
    """Secondary guard: read the actual web_worker.py source at the
    pickup call-site and assert the `bounce=` kwarg is derived from
    `self._no_detect_retries > 0`. This makes sure the flag logic in
    the real code matches what `test_bounce_flag_from_no_detect_retries`
    validates in isolation.
    """
    worker_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "web_worker.py",
    )
    with open(worker_path, "r", encoding="utf-8") as f:
        src = f.read()

    # The source-bin pickup should pass `bounce=self._no_detect_retries > 0`.
    # Anchor on the X_SOURCE_BIN argument so we don't accidentally match
    # an unrelated `pick_from_position(...)` call.
    assert "gcode_control.X_SOURCE_BIN" in src
    assert "bounce=self._no_detect_retries > 0" in src, (
        "web_worker.py source-bin pickup must pass "
        "bounce=self._no_detect_retries > 0"
    )
