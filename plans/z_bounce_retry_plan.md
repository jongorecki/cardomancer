# Z-Bounce Retry Plan — Unsticking Cards on No-Detect

## Problem

When the vacuum picks from the source bin, multiple stuck-together cards
sometimes come off as one blob (the top-of-bin tabs can't break the suction
between cards when the micro-pump's grip is stronger than the separation
force). The result: the card dropped on staging is oversized / misaligned /
absent, `detect_card()` returns `None`, and we log a no-detect.

Today (web_worker.py:1121–1145) the response is:

1. Increment `_no_detect_retries`.
2. Re-enqueue `detect_and_sort`, which re-runs
   `pick_from_position(X_SOURCE_BIN)` → drop → detect.
3. After 3 consecutive misses (`_max_no_detect_retries = 3`), stop continuous
   sorting with reason `no_card` (web_worker.py:1130–1136).

> **Revised 2026-04-22:** an earlier draft of this plan pointed at
> `pick_from_source()` (gcode_control.py:839). That function exists but is
> **not** on the continuous-sort call path — nothing in the worker calls it.
> The real pickup call is
> `gcode_control.pick_from_position(gcode_control.X_SOURCE_BIN)` at
> web_worker.py:1052, which dispatches to `pick_from_position()` at
> gcode_control.py:963. All references below have been updated to match
> the real call path. Because `pick_from_position` is generic (also used
> for staging and other surfaces), the `bounce` flag is opt-in per caller
> — only the source-bin call site passes it.

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
- `pick_from_position()` takes a `bounce: bool = False` flag.
- `web_worker._cmd_detect_and_sort` passes `bounce=True` when
  `_no_detect_retries > 0` at the source-bin call site.
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

**Modify `pick_from_position()`** (gcode_control.py:963) to accept a flag:

```python
def pick_from_position(x_position, bounce: bool = False):
    ...
    # Probe down to contact (fast approach if cached)
    _probe_with_cache(x_position)           # gcode_control.py:977
    # Vacuum on, wait for grip
    _send_and_wait("M106 P0 S255")          # gcode_control.py:979
    _send_and_wait(f"G4 P{VACUUM_ON_DELAY_MS}")  # gcode_control.py:980

    # On retry: try to shake loose any stuck cards below the top one.
    # We're still at probe-contact Z; extras fall back onto the stack
    # during the down pulses.
    if bounce:
        _z_bounce_at_contact(x_position)

    # Lift
    _send_and_wait(f"G0 Z{z_clear} F{Z_FEEDRATE}")  # gcode_control.py:982
    _send_and_wait("M400")                           # gcode_control.py:983
```

**New helper** `_z_bounce_at_contact(x_position)`:

- Read `contact_z = _probe_z_cache[x_position]` — `_probe_with_cache`
  (gcode_control.py:574) updates this dict on every probe, so after the
  probe call above, the value is guaranteed present for the current
  x_position.
- For `i in range(Z_BOUNCE_COUNT)`:
  - `G0 Z{contact_z + Z_BOUNCE_DISTANCE_MM} F{Z_BOUNCE_FEEDRATE}` + `M400`
  - `G0 Z{contact_z} F{Z_BOUNCE_FEEDRATE}` + `M400`
- Print/log `[gcode] Z-bounce x{N} completed at x={x_position}`.

**Safety checks:**
- `contact_z + Z_BOUNCE_DISTANCE_MM` must not exceed `Z_CLEAR_HEIGHT` /
  `Z_MAX` (it won't at 3mm, but assert it anyway — cheap).
- Bounce happens only after the vacuum-on delay has fully elapsed; do not
  move the cup while it's still forming its grip.
- Every Z move ends with `M400`. No overlapping X moves — there shouldn't
  be any X moves in this block, but call that out explicitly in the helper's
  docstring so future edits don't break it.

### Step 2 — Wire it up in the worker (`web_worker.py`)

The source-bin pickup call is at **web_worker.py:1052** inside
`_cmd_detect_and_sort` (web_worker.py:980). Change:

```python
gcode_control.pick_from_position(gcode_control.X_SOURCE_BIN)
```

to:

```python
if self._no_detect_retries > 0:
    self.log(f"Retry pickup with Z-bounce "
             f"(miss {self._no_detect_retries}/{self._max_no_detect_retries})")
gcode_control.pick_from_position(
    gcode_control.X_SOURCE_BIN,
    bounce=self._no_detect_retries > 0,
)
```

Do **not** touch other `pick_from_position` call sites (staging pickups,
diagnostic callers, etc.). They stay on the default `bounce=False` path.

The counter increment at web_worker.py:1122 and the reset at :1148 stay as-is.

### Step 3 — Test

Follow the `benchmark_motion.py` pattern (there are no motion *unit* tests
today; motion is exercised via integration scripts with a real/simulated
serial).

Add `tests/test_z_bounce.py`:

1. **G-code sequence test** — patch `_send_and_wait` and `_probe_with_cache`
   (the latter so the test doesn't need a real serial + probe; stub it to
   seed `_probe_z_cache[x]` with a known value). Call
   `pick_from_position(X_SOURCE_BIN, bounce=True)` and assert:
   - `M106 P0 S255` precedes any bounce move
   - `Z_BOUNCE_COUNT` pairs of `G0 Z...` down/up appear between vacuum-on
     and the final lift
   - Every bounce Z move is followed by `M400`
   - No `G0 X...` appears between vacuum-on and final lift
2. **No-bounce default** — `pick_from_position(X_SOURCE_BIN)` (no
   `bounce` arg) produces the same command list as before this change.
   Regression guard for the common-case path and for unrelated callers
   (staging pickups, etc.) that never pass `bounce=True`.
3. **Worker wiring** — patch `gcode_control.pick_from_position`, drive
   `_cmd_detect_and_sort` through 2 consecutive no-detect outcomes (mock
   `detect_card` to return `None` both times), assert the first call got
   `bounce=False` and the second got `bounce=True`. If driving the full
   worker is too heavy, an equivalent unit test on just the call-site
   logic (constructing a worker, setting `_no_detect_retries`, asserting
   the kwarg passed) is acceptable.

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
  `pick_from_position` beyond adding the flag and the bounce block.
- **test-runner agent** — after the impl agent reports done, run
  `tests/test_z_bounce.py` + any existing scan_tracker/worker tests and
  report pass/fail.
- **No detection-diag or frame-inspector involvement** — this is a motion
  change; detection code is untouched.

The impl agent must NOT commit or branch on its own (per the
enrichment-impl commit rule; same discipline applies here). It should
produce the diff and stop so the human can review before commit.

## Acceptance criteria

- [ ] `pick_from_position(x)` (no `bounce` arg) emits the same G-code
      as today.
- [ ] `pick_from_position(x, bounce=True)` emits `Z_BOUNCE_COUNT`
      down/up pulses between vacuum-on and the final lift, each sync'd
      with M400.
- [ ] No X move appears anywhere in the bounce block.
- [ ] The source-bin call site (web_worker.py:1052) passes
      `bounce=True` iff `_no_detect_retries > 0`. All other
      `pick_from_position` call sites are unchanged.
- [ ] A log line in the session log identifies when a bounce retry
      fired, including the current miss count.
- [ ] New tests pass; existing tests unaffected.

## Key references

- Counter state: web_worker.py:116–118
- Source-bin pickup call site: web_worker.py:1052
- No-detect branch: web_worker.py:1121–1145
- Counter reset on success: web_worker.py:1148
- Pickup primitive: gcode_control.py:963–983 (`pick_from_position`)
- Bounce insertion point: between gcode_control.py:980 and :982
- Probe cache (contact Z lookup): gcode_control.py:574–597
  (`_probe_with_cache` writes `_probe_z_cache[x_position]`)
- Motion constants block: gcode_control.py:22–131
- M400 / no-X-during-Z convention: `feedback_no_zx_overlap` memory
