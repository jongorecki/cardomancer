# Cycle Time Findings — 2026-07-19

**Nothing has been changed.** This is an observation record only. All items below are
proposals for review.

Context: measured cycle time 15–18 s/card, ~240 cards/hr. Assumption going in was that
X traverse dominates. The config suggests otherwise.

---

## Finding 1 — G38 contact descents are running at 5 mm/s (likely the whole problem)

`Configuration.h:1667`
```c
#define Z_PROBE_FEEDRATE_FAST  (5*60) // (mm/min)
#define Z_PROBE_FEEDRATE_SLOW  (Z_PROBE_FEEDRATE_FAST / 2)
```

`(5*60)` mm/min = 300 mm/min = **5 mm/s**. Slow probe = 2.5 mm/s.

Every G38.2 descent onto a card runs at this rate for its entire travel. At 5 mm/s:

| Descent distance | Time |
|---|---|
| 20 mm | 4 s |
| 40 mm | 8 s |
| 60 mm | 12 s |

There are at least two contact descents per card (pick at source, re-pick at staging).
Two descents of 40 mm = **16 s**, which matches the observed cycle almost exactly.

**Proposed fix (not implemented): two-stage descent.**
The machine already knows where it made contact on the previous card. A card is ~0.3 mm
thick, so the next contact is ~0.3 mm lower. Therefore:

1. Rapid `G1 Z` at full Z feedrate (75 mm/s) down to `last_contact_Z - 2 mm`
2. `G38.2` for only the remaining ~3–5 mm

Probe time drops from ~8 s to well under 1 s. Rapid descent of 40 mm at 75 mm/s is
~0.6 s. Total per descent ≈ 1.5 s instead of 8 s.

Needs a safe fallback: if contact isn't made within the expected window, back off and
re-probe from higher with a longer G38 travel. First card of a session, and any
re-home, uses the slow full-travel probe to establish the reference.

Estimated effect: **15–18 s → 5–7 s** on this change alone.

---

## Finding 2 — S-curve acceleration is disabled

`Configuration.h`
```c
//#define S_CURVE_ACCELERATION
```

Commented out. Worth knowing, since the working assumption was that this had been
tuned. On a machine with a marginal X motor (0.9 A A8 stepper on 1100 mm of travel),
S-curve is specifically useful — it reduces the instantaneous torque demand at the start
of a move, which is where step-skipping originates. May allow a higher accel ceiling
than 500 without skipping.

---

## Finding 3 — Travel acceleration is being clamped

```c
#define DEFAULT_MAX_ACCELERATION    { 500, 500, 500, 5000 }
#define DEFAULT_TRAVEL_ACCELERATION 2000
```

`DEFAULT_TRAVEL_ACCELERATION` of 2000 is clamped by the per-axis max of 500. Effective
X travel acceleration is **500 mm/s²**, not 2000.

At 500 mm/s², a 300 mm move is fully acceleration-limited (triangular profile, never
reaches the 500 mm/s max feedrate):

- `t = 2·√(d/a) = 2·√(0.3/500)`… → **1.55 s**, peak velocity 387 mm/s
- At 2000 mm/s²: **0.77 s**

Roughly half the traverse time is available here, *if* the X motor can take it. It
probably can't at 0.9 A — this is gated on the 1.5–1.7 A replacement already on order,
and on Finding 2.

---

## Finding 4 — Junction deviation is very conservative

```c
#define JUNCTION_DEVIATION_MM 0.08
```

0.08 mm is a 3D-printing value chosen to protect surface finish. This machine carries a
card on a suction cup; the relevant constraint is card slip, not extrusion quality.
0.2–0.5 is likely safe and would materially improve cornering. Requires a slip test.

Related: "blended cornering isn't possible in the current design" may be a host-side
command-flow issue rather than a firmware one. Marlin blends consecutive queued moves
through junctions automatically **unless** something forces a sync between them. If
`gcode_control.py` issues `M400` or waits for idle after every move, every waypoint
becomes a full stop. Worth checking before concluding the firmware can't do it.

---

## Priority ordering (proposed)

1. **Two-stage probe descent** — biggest win by a wide margin, host-side software only,
   no firmware reflash
2. **Check for unconditional `M400`/wait-for-idle in `gcode_control.py`** — free if present
3. Bottom-side vision (removes one of the two contact descents entirely, plus the
   staging place/re-pick) — compounds with #1
4. S-curve + accel ceiling, gated on the new X motor
5. Junction deviation, gated on a slip test
6. Speculative bin commit — see `speculative_bin_commit_PLAN.md`, gated on cycle < 4 s
