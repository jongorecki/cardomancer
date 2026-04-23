# Z-Bounce Retry Plan — Unsticking Cards on No-Detect

## Problem

When the vacuum picks from the source bin, multiple stuck-together cards
sometimes come off as one blob (the top-of-bin tabs can't break the suction
between cards when the micro-pump's grip is stronger than the separation
force). The result: the card dropped on staging is oversized / misaligned /
absent, `detect_card()` returns `None`, and we log a no-detect.

Today (web_worker.py:1121–1145) the response is:

1. Increment `_no_detect_retries`.
2. Re-enqueue `detect_and_sort`, which re-runs `pick_from_source()` → drop →
   detect.
3. After 3 consecutive misses (`_max_no_detect_retries = 3`), stop continuous
   sorting with reason `no_card` (web_worker.py:1130–1136).

Nothing in that retry *physically attempts to separate the stuck stack*, so
the same blob tends to come back up on the next try and we burn all 3 retries.

## Fix

On retry pickups (i.e. when `_no_detect_retries > 0`), after the vacuum is
on and the suction head is at card-contact Z **inside the source bin**, do
a short Z-bounce (a few small up/down Z pulses) before the final lift. The
pulses jolt the stack vertically — the direction the suction is actually
acting — so any extras fall back into the bin while the top card stays on
the cup.

The bounce is **not** performed on the first pickup of a scan; only on
retries. That keeps the common case at normal throughput and only pays the
bounce cost when we already know something is wrong.

## Scope

### In scope
- New motion primitive: Z-bounce sequence inside the source bin, vacuum on,
  respecting M400 sync between every Z move (no X/Z overlap — per
  `feedback_no_zx_overlap`).
- `pick_from_source()` takes a `bounce: bool = False` flag.
- `web_worker._do_detect_and_sort` passes `bounce=True` when
  `_no_detect_retries > 0`.
- Config constants in `gcode_control.py` for bounce distance, count, feedrate.
- Log line per bounce attempt so scan_logs show when it fired.
- Unit/integration test exercising the bounce G-code sequence.

### Out of scope
- Any vacuum-pulse-release strategy (modulating M106 S255 → S0 → S255 mid-pick).
  Pure Z-bounce first; revisit only if bounce alone proves insufficient.
- Changes to `_max_no_detect_retries` or the stop-on-empty-source behavior.
- X-axis shake (rejected: suction force is along Z).
- Bounce at staging drop or anywhere else in the pipeline.

## Implementation

### Step 1 — Motion primitive (`gcode_control.py`)

**Add constants** in the config block at the top (alongside
`VACUUM_ON_DELAY_MS` at line 130):

```python
# Z-bounce: executed on retry pickups to break suction between stuck cards.
# Bounce is performed inside the source bin, vacuum ON, at probe-contact Z.
Z_BOUNCE_DISTANCE_MM = 3.0   # how far up/down each pulse travels
Z_BOUNCE_COUNT = 3           # number of up/down pulses
Z_BOUNCE_FEEDRATE = 4000     # mm/min — slower than travel for sharper jolt
```

**Modify `pick_from_source()`** (gcode_control.py:839) to accept a flag:

```python
def pick_from_source(bounce: bool = False):
    ...
    # Vacuum on, wait for grip
    _send_and_wait("M106 P0 S255")
    _send_and_wait(f"G4 P{VACUUM_ON_DELAY_MS}")

    # On retry: try to shake loose any stuck cards below the top one.
    # We're still inside the source bin at contact Z; extras fall back
    # into the bin during the down pulses.
    if bounce:
        _z_bounce_in_source()

    # Lift to clear height
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")
    _send_and_wait("M400")
```

**New helper** `_z_bounce_in_source()`:

- Read current Z via `M114` (or re-use the probe cache value — whichever is
  already the convention near `_probe_with_cache`).
- For `i in range(Z_BOUNCE_COUNT)`:
  - `G0 Z{contact_z + Z_BOUNCE_DISTANCE_MM} F{Z_BOUNCE_FEEDRATE}` + `M400`
  - `G0 Z{contact_z} F{Z_BOUNCE_FEEDRATE}` + `M400`
- Print/log `[gcode] Z-bounce x{N} completed at source`.

**Safety checks:**
- `contact_z + Z_BOUNCE_DISTANCE_MM` must not exceed `Z_CLEAR_HEIGHT` /
  `Z_MAX` (it won't at 3mm, but assert it anyway — cheap).
- Bounce happens only after the vacuum-on delay has fully elapsed; do not
  move the cup while it's still forming its grip.
- Every Z move ends with `M400`. No overlapping X moves — there shouldn't
  be any X moves in this block, but call that out explicitly in the helper's
  docstring so future edits don't break it.

### Step 2 — Wire it up in the worker (`web_worker.py`)

Find the call site of `pick_from_source()` that runs inside
`_do_detect_and_sort` (the pickup-then-drop-then-detect flow used by
continuous sort). Change:

```python
gcode_control.pick_from_source()
```

to:

```python
gcode_control.pick_from_source(bounce=self._no_detect_retries > 0)
```

Add a log line when bouncing is active so it shows up in scan_logs:

```python
if self._no_detect_retries > 0:
    self.log(f"Retry pickup with Z-bounce "
             f"(miss {self._no_detect_retries}/{self._max_no_detect_retries})")
```

The counter increment at web_worker.py:1122 and the reset at :1148 stay as-is.

### Step 3 — Test

Follow the `benchmark_motion.py` pattern (there are no motion *unit* tests
today; motion is exercised via integration scripts with a real/simulated
serial).

Add `tests/test_z_bounce.py`:

1. **G-code sequence test** — patch the serial `_send_and_wait` to capture
   the command list, call `pick_from_source(bounce=True)`, and assert:
   - `M106 P0 S255` precedes any bounce move
   - `Z_BOUNCE_COUNT` pairs of `G0 Z...` down/up appear between vacuum-on
     and final lift
   - Every bounce Z move is followed by `M400`
   - No `G0 X...` appears between vacuum-on and final lift
2. **No-bounce default** — `pick_from_source()` (no arg) produces the
   identical command list as before this change. This is the regression
   guard for the common-case path.
3. **Worker wiring** (in `tests/test_scan_tracker.py` or a new worker test)
   — patch `gcode_control.pick_from_source`, drive `_do_detect_and_sort`
   through 2 consecutive misses, assert the first call has `bounce=False`
   and the second has `bounce=True`.

### Step 4 — Manual validation (human, not agent)

Once (1)–(3) pass, the user runs a live test with a deliberately stuck
pair of cards in the source bin:

- Confirm the bounce is audible/visible at the correct point (inside the
  bin, after vacuum on, before lift).
- Confirm it actually separates the stack in a meaningful fraction of
  retries. If it doesn't, we revisit with vacuum-pulse release (out of
  scope here).

## Agent assignments

This is small and mostly lives in two files, so it doesn't need to be
split across many agents.

- **general-purpose agent** — implement Steps 1–3 (motion primitive,
  worker wiring, tests). Keep the change minimal; do not refactor
  `pick_from_source` beyond adding the flag and the bounce block.
- **test-runner agent** — after the impl agent reports done, run
  `tests/test_z_bounce.py` + any existing scan_tracker/worker tests and
  report pass/fail.
- **No detection-diag or frame-inspector involvement** — this is a motion
  change; detection code is untouched.

The impl agent must NOT commit or branch on its own (per the
enrichment-impl commit rule; same discipline applies here). It should
produce the diff and stop so the human can review before commit.

## Acceptance criteria

- [ ] `pick_from_source(bounce=False)` emits the same G-code as today.
- [ ] `pick_from_source(bounce=True)` emits `Z_BOUNCE_COUNT` down/up
      pulses between vacuum-on and the final lift, each sync'd with M400.
- [ ] No X move appears anywhere in the bounce block.
- [ ] Worker calls `pick_from_source(bounce=True)` iff
      `_no_detect_retries > 0`.
- [ ] A log line in the session log identifies when a bounce retry
      fired, including the current miss count.
- [ ] New tests pass; existing tests unaffected.

## Key references

- Counter state: web_worker.py:116–118
- No-detect branch: web_worker.py:1121–1145
- Counter reset on success: web_worker.py:1148
- Pickup sequence: gcode_control.py:839–871
- Bounce insertion point: between gcode_control.py:865 and :868
- Motion constants block: gcode_control.py:22–131
- M400 / no-X-during-Z convention: `feedback_no_zx_overlap` memory
