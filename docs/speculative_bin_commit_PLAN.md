# Speculative Bin Commit — Implementation Plan

**Status:** Proposal, not scheduled
**Author:** drafted 2026-07-19
**Prereq:** See "Gate" below. Do not build this yet.

---

## 0. Gate — read this first

The idea: after the camera frame is captured but *before* identification finishes,
start moving X toward the bin the card will most likely land in. If we guessed right,
the ~300–500 ms of identification is hidden inside a move we had to make anyway.

**Maximum possible saving is one identification interval per card: 0.3–0.5 s.**

Current measured cycle time is **15–18 s**. So the ceiling on this optimization is
about **2–3% of cycle time**, and only when the guess is correct.

Meanwhile `Z_PROBE_FEEDRATE_FAST` in `Configuration.h` is `(5*60)` mm/min = **5 mm/s**,
and every G38.2 contact descent runs at that rate. A 50 mm blind descent costs 10 s.
That is very likely 60–80% of the cycle.

**Do not build speculative commit until the probe-descent fix is in and cycle time is
under ~4 s.** Below 4 s, a 0.4 s saving is 10%+ and worth the complexity. Above 10 s,
this is decoration on the wrong problem.

Written up now so it's ready when the gate opens.

---

## 1. The decision problem

At capture time we know:

- The active sort rule (bin assignment function), from `bin_configs/*.json`
- Current head position (staging X)
- Bin X positions
- The empirical history of where cards from this input stack have gone

We do not know the destination bin. We want to choose a provisional X target `X*`
that minimizes expected total time to reach the true destination.

Three candidate policies:

| Policy | Behavior | Risk |
|---|---|---|
| **Wait** | Hold at staging until ID lands, then one clean move | None. Current behavior. |
| **Commit** | Move to the single most likely bin | If wrong: full stop + reverse |
| **Hedge** | Move to the probability-weighted median of bin positions | Never reverses if the median lies between candidates |

**Recommended: Hedge, with Commit as an opt-in when confidence is very high.**

Rationale: for L1 (distance) cost on a single axis, the point minimizing expected
remaining travel is the **weighted median** of bin X positions, not the mean. It is
also monotone-safe — moving toward the weighted median never moves us away from more
than half the probability mass.

---

## 2. Bin probability model

### 2.1 The quantity we need

We need `P(bin_j)` for the *next* card, not for a specific card. Decompose:

```
P(bin_j) = Σ  P(card c is next)
          c ∈ cards that route to bin_j
```

And per-card probability from rarity:

```
P(card c) ∝ pack_rate(rarity(c), set(c)) / n_cards(rarity(c), set(c))
```

This is the "how many possible cards per bin" intuition — a bin catching all 20 mythics
of a set gets `20 × mythic_per_card_rate`; a bin catching 101 commons gets
`101 × common_per_card_rate`. The counts matter as much as the rates.

### 2.2 Rarity pack rates

**These numbers must be verified before use — do not trust them as written.**
Collation changed materially with Play Boosters (MKM, 2024 onward) and varies by
product (draft / set / collector / jumpstart). Put them in config, not in code.

Approximate legacy draft booster, 15 cards:

| Rarity | Per pack | Share of non-land slots |
|---|---|---|
| Common | ~10 | ~71% |
| Uncommon | ~3 | ~21% |
| Rare | ~0.875 | ~6.2% |
| Mythic | ~0.135 | ~1.0% |

Mythic appears roughly 1 in 7.4 packs. Play Boosters shift commons down and add a
wildcard-rarity slot and a guaranteed foil slot, which flattens the distribution.

Source of truth: Wizards' published collation notes per set, plus Scryfall for
`n_cards(rarity, set)` — we already ingest Scryfall bulk data
(`default-cards-*.json`), so the per-set rarity counts are free.

### 2.3 The prior is the weak part — lean on observation instead

Real input stacks are **not** freshly opened packs. They are bulk boxes, draft chaff,
traded collections, someone's shoebox. The pack-rate prior describes the wrong
generative process for most of what we actually sort.

**The empirical distribution of the last N cards from the current stack is a far better
predictor than any collation math.** Use collation only as a cold-start prior.

Formulation — Dirichlet-multinomial:

```
α_j(0) = κ · P_prior(bin_j)        # κ ≈ 10, weak prior
α_j(t) = α_j(0) + count of cards routed to bin_j so far this session

P(bin_j) = α_j(t) / Σ_k α_k(t)
```

After ~30 cards the observations dominate, which is what we want. Add exponential
decay (`α ← 0.99·α` per card) if a stack is expected to be non-stationary — e.g.
sorted-by-set input where the distribution shifts partway through.

---

## 3. Move policy

### 3.1 Weighted median target

```python
def hedge_target(bin_probs: dict[int, float], bin_x: dict[int, float]) -> float:
    items = sorted(bin_x.items(), key=lambda kv: kv[1])   # by X position
    cum = 0.0
    for bin_id, x in items:
        cum += bin_probs.get(bin_id, 0.0)
        if cum >= 0.5:
            return x
    return items[-1][1]
```

### 3.2 Commit mode

Use the argmax bin instead of the median when `max(P) > p_threshold`.

Break-even for `p_threshold`, where `T_save` is time recovered on a correct guess and
`T_penalty` is the extra decel+reaccel+reverse-travel cost on a wrong guess:

```
p · T_save  >  (1 - p) · T_penalty
p*  =  T_penalty / (T_save + T_penalty)
```

Measure both terms on the machine rather than assuming. If `T_penalty ≈ T_save`,
`p* = 0.5`. Start conservative at `p* = 0.75`.

### 3.3 Marlin will blend the two moves for us — if we let it

Important and easy to get wrong. Marlin's planner blends consecutive queued moves
through the junction **as long as nothing forces a sync between them**. So:

```
G1 X<hedge_target> F<travel>     <- queued, starts executing
   ... identification completes ...
G1 X<true_bin> F<travel>         <- queued while the first is still running
```

If the second `G1` reaches the planner before the first one drains, the head flows
through `hedge_target` without stopping. That makes a correct-direction hedge
effectively free.

**This will not happen if we emit `M400` (or wait for move-complete) after the first
move.** Check `gcode_control.py` for unconditional `M400` / wait-for-idle after every
move — that is what turns every waypoint into a full stop, and it is the same reason
blended cornering "isn't possible" in the current design. It probably is possible; it's
a host-side command-flow issue, not a firmware limitation.

Also note `JUNCTION_DEVIATION_MM 0.08` is very conservative. For a machine carrying a
card on a suction cup rather than extruding plastic, 0.2–0.5 is likely fine and will
markedly improve cornering. Test for card slip on the cup before raising it.

---

## 4. Implementation phases

**Phase 1 — Instrumentation only (no behavior change).**
Log per card: capture timestamp, ID-complete timestamp, destination bin, X distance
travelled, move start/end timestamps. Compute the actual `T_inference`, `T_move`, and
the empirical bin distribution per session. Without this we're guessing at whether the
optimization pays.

**Phase 2 — Offline replay.**
Against logged sessions, replay the hedge and commit policies. Report: hit rate,
simulated time saved, time lost to wrong guesses, net. Decide go/no-go on real numbers.

**Phase 3 — Predictor module.**
`bin_predictor.py`, no motion coupling:
```python
class BinPredictor:
    def __init__(self, bin_config, rarity_prior, kappa=10.0, decay=1.0): ...
    def observe(self, bin_id: int) -> None: ...
    def probabilities(self) -> dict[int, float]: ...
    def hedge_target(self) -> float: ...
    def commit_target(self) -> tuple[int, float] | None: ...   # None if below p*
```
Unit-testable, no serial, no camera.

**Phase 4 — Wire into `web_worker.py` behind a feature flag.**
`speculative_commit: {enabled: false, mode: "hedge", p_threshold: 0.75}` in the session
config. Default off. A/B by alternating sessions.

**Phase 5 — Verify.**
Compare cycle time and misplacement rate flag-on vs flag-off over ≥200 cards each.
Watch specifically for cards slipping on the cup during the mid-move direction change.

---

## 5. Failure modes to design against

| Risk | Mitigation |
|---|---|
| Card slips on cup during hedge→redirect | Cap jerk/junction deviation for card-carrying moves; verify with a slip test at max redirect |
| Identification fails / needs reject bin | Reject bin must be in the probability model, not a special case |
| Wrong guess cascades into a timeout | Hard cap: if ID hasn't landed by `T_move_hedge`, stop and wait — never move a second time on speculation |
| Predictor drifts on a mixed stack | Exponential decay on α; reset predictor on session start |
| Distribution collapses to one bin | That's the *good* case — commit mode will fire nearly always |

---

## 6. Open questions

1. What is the actual measured identification time? Believed 0.3–0.5 s, unverified.
2. Where is the `M400` / wait-for-idle in `gcode_control.py`, and can the hedge move and
   final move be queued back to back without a sync?
3. Is the input stack ever pre-sorted in a way that makes the distribution
   non-stationary within a session?
4. Do we want the predictor to condition on anything cheap that's available before full
   ID completes — e.g. a fast border/color-signature pass at 20 ms that narrows the bin
   set before the embedding lands? That could beat the rarity prior outright.

Question 4 may be the better version of this whole idea: a cheap early-exit classifier
that gets bin identity right 90% of the time in 20 ms is strictly better than a
statistical prior, and the cascade infrastructure already exists
(`bench_cascade_stages.py`).
