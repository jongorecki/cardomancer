# Printing Disambiguation Plan

## STATUS

Phases 1, 2, and 3 complete (2026-04-22). Phases 4–7 pending.

Being built in parallel with foil detection (`plans/foil_detection_plan.md`)
on branch `feature/printing-disambiguation`.

Phases are numbered in **build order**. Read top to bottom.

---

## Problem

phash matches a card to its art. Several cards share identical (or
near-identical) art across many printings — core-set staples, Commander
reprints, Secret Lair reuses, The List, retro-frame treatments,
borderless variants, etc. When phash returns multiple candidates that
share the same `illustration_id`, we currently fall back to the
"cheapest printing" index (`_cheapest_printing_index` in `cards.py`).
That's a guess, not an identification.

Known-wrong outcomes:
- A DMR Sol Ring gets logged as a C19 Sol Ring (wrong price, wrong set
  icon, wrong legality context for pricing deltas)
- A The List reprint gets logged as its original printing
- A retro-frame treatment gets logged as the modern-frame printing
  (different price, different collector appeal)
- A borderless variant gets logged as the regular printing
- Enrichment data (buylists, staple tiers) attaches to the wrong
  set/SKU

**Not a detection-accuracy signal.** phash has already identified the
card / `oracle_id` with high confidence. Everything in this plan is a
**tiebreaker** across printings that share art. If phash returns a
single confident match, we skip all of this.

## Key insight

Because phash gives us the candidate printing list, this is **not** a
"classify arbitrary printings" problem. It's a "pick among 2–10 known
candidates" problem. Scryfall tells us what each candidate looks like
(`frame`, `frame_effects`, `set` code, `border_color`, whether it's on
The List by set_code=`plst`), so every check below is "does the scan
match this known option." No trained classifier needed — just
constrained template matching and feature sampling.

## Three disambiguation signals (built in this order)

Each is run only when the phash candidate list disagrees on that
specific axis. Shipping in order of simplicity; each stage narrows
the candidate list, and anything surviving all three stages (or
falling through an ambiguous stage) goes to the existing
cheapest-printing fallback.

| Stage | Signal | What it picks between | Complexity |
|-------|--------|----------------------|-----------|
| 1 | **Frame / border** | `frame` era (1997/2003/2015), `frame_effects` (borderless, retro, showcase, extendedart) | Simplest — huge signal, broad ROIs |
| 2 | **The List stamp** | `plst` reprint vs. original printing of same art | Simple — binary, one template, one ROI |
| 3 | **Set icon** | Which `set_code` among multiple same-frame reprints | Hardest — small ROI, hundreds of templates, some confusable pairs |

## Scope

**In scope**
- Disambiguate which printing of a card we have when phash returns ≥2
  candidates sharing `illustration_id` at close hash distance
- Three detection modules: frame/border, The List stamp, set icon
- Shared asset pipeline (Scryfall symbols, reference frames, List stamp
  template) and shared ROI lookup infrastructure
- Integration into the existing identification flow as a tiebreaker
  cascade
- Validation harness + `diag_printing.py` diagnostic script

**Out of scope**
- Classifying arbitrary sets from cards with no phash match
  (always start from the phash candidate list)
- Disambiguating same-set variants other than The List (promo stamps,
  pre-release stamps, etched vs. regular foil within one set) — future
  follow-up
- Using rarity color as signal for set icons (foils, low light, and
  color casts make it unreliable; we deliberately discard color)
- Replacing the existing `_cheapest_printing_index` default — this
  augments it, doesn't replace it. When all three detectors return low
  confidence, fall back to the current cheapest-printing logic.
- Fixing hash-DB coverage gaps — that's the frame-dedup fix in
  `plans/foil_detection_plan.md` Priority 1. If that ships first,
  phash itself may already distinguish frames, and Phase 1 becomes a
  cross-check rather than a primary signal. Build Phase 1 anyway; it
  catches same-`frame` / different-`frame_effects` cases phash misses.

---

## Phase 1: Frame / Border Detection — DONE 2026-04-22

**Module:** `frame_detect.py`
**Tests:** `tests/test_frame_detect.py` (10 tests, passing)
**Commit:** `d38e013` on `feature/printing-disambiguation`

### Approach

Feature sampling in broad ROIs rather than template matching. The
frame covers most of the card so signal is huge and detection is
easy. Reuses the existing `frame_signatures.json` reference data
(built by `find_frame_signatures.py`) which stores border-region
phashes per `(frame, frame_effects)` combo.

### API

```python
def detect_frame(
    card_img: np.ndarray,
    candidate_combos: Optional[Iterable[tuple[str, tuple[str, ...]]]] = None,
    debug: bool = False,
) -> dict:
    """Returns {best_frame, best_frame_effects, distance, margin,
    confidence, scores}. Restricts scoring to candidate_combos when
    provided — the realistic use case when phash has returned a
    candidate printing list."""
```

### Algorithm

1. Compute phash of three border regions on the scan: top 50 px strip,
   left 50 px strip, bottom 50 px strip.
2. For each candidate `(frame, frame_effects)` combo, compute minimum
   Hamming distance from each scan region phash to any reference phash
   of that combo's corresponding region; sum across 3 regions.
3. Return the combo with lowest total distance; margin = distance to
   second-best; confidence = margin / scale (default scale = 30).

### Constants

```python
_CONFIDENCE_MARGIN_SCALE = 30.0  # empirically chosen, tune from diag data
```

### Remaining polish (not blocking)

- Real-scan validation on labeled data (Phase 7)
- Tune `_CONFIDENCE_MARGIN_SCALE` once we see realistic margin
  distributions on scan data
- Consider adding a rectified-scan (not downloaded-image) test fixture
  — current tests use source images that already live in the
  reference set, so distance=0 is trivial to achieve

---

## Phase 2: The List Stamp Detection — DONE 2026-04-22

**Module:** `list_stamp.py`
**Template:** `card_data/stamps/the_list.png` (28×28 grayscale)
**Tests:** `tests/test_list_stamp.py` (10 tests, passing)
**Commit:** `7f6c21a` on `feature/printing-disambiguation`

The List (`plst`) and Unfinity's list (`ulst`) reprint cards with the
same art as the original printing but add a planeswalker-symbol stamp
just above the collector-info line. Binary detection: stamp present →
list-style reprint, stamp absent → original printing. The detector
does **not** distinguish `plst` from `ulst`; the cascade's gating
condition scopes usage to cases where phash candidates include
one list-style set and one non-list set of the same art.

### Key decisions vs. the original draft

- Template uses **grayscale directly, not Canny edges**. The stamp is
  already a high-contrast white-on-black shape; Canny edges of the
  stamp are too sparse (~20 nonzero pixels) for reliable
  `TM_CCOEFF_NORMED` scoring.
- Template cropped from **one clean source image** rather than
  averaged across multiple. Phase-correlation alignment of source
  images introduced noise; a single clean source gave stronger
  separation.
- ROI tightened to `(0, 968, 35, 30)` (tight around stamp) instead of
  the original draft's `(30, 960, 80, 70)`. Widening the ROI
  dramatically increased false positives because collector-line text
  shapes score highly against the stamp template.
- Threshold `LIST_STAMP_THRESHOLD = 0.35` (vs draft 0.55) — tuned on
  ~2200 source images: list-style p10 ≈ 0.37, non-list p99 ≈ 0.0.
- Added `is_list_candidate_pair(candidates)` helper for Phase 3's
  gating. Checks `LIST_LIKE_SETS = {"plst", "ulst"}` against each
  candidate.

### API (as shipped)

```python
def detect_list_stamp(
    card_img: np.ndarray,
    debug: bool = False,
) -> tuple[bool, float]:
    """Detect whether the scan has a list-style planeswalker stamp.
    Returns (has_stamp, confidence)."""
```

### Remaining polish (not blocking)

- Real-scan validation on labeled data (Phase 7)
- Tune threshold once real scans are available — current tuning uses
  Scryfall source images, not camera scans
- Extend `LIST_LIKE_SETS` as new list-style reprint sets ship

---

## Phase 3: Integration Cascade Scaffolding (Stages 1 + 2)

Wire Phase 1 and Phase 2 into the identification finalization path.
Set-icon stage is stubbed — just a no-op that passes the candidate
list through, to be filled in by Phase 6.

### Hook point

In `card_identify.py` / `card_identify_hybrid.py`, post-phash match
and pre-return. See Phase 1 mapping notes for exact line.

### Cascade structure

```python
candidates = phash_match(card_img)

if len(candidates) == 1:
    return candidates[0], source="single_match"

same_art = [c for c in candidates if _same_illustration_id(c, candidates[0])]
if len(same_art) < 2:
    return candidates[0], source="single_match"

# Stage 1: frame / border
if _candidates_disagree_on_frame(same_art):
    result = detect_frame(card_img, candidate_combos=_combos_from(same_art))
    if result["confidence"] > FRAME_THRESHOLD:
        same_art = _filter_by_frame(same_art, result["best_frame"],
                                    result["best_frame_effects"])

# Stage 2: The List stamp
if _candidates_include_list_and_original(same_art):
    is_list, conf = detect_list_stamp(card_img)
    if conf > LIST_STAMP_THRESHOLD:
        same_art = _filter_by_list(same_art, is_list)

# Stage 3: set icon — stubbed until Phase 6
# (will call identify_set_icon() here)

if len(same_art) == 1:
    return same_art[0], source="disambiguated"
return _cheapest_printing_fallback(same_art), source="cheapest_fallback"
```

### Result field additions

- `detected_set_code: str`
- `detected_frame: str` (when Stage 1 committed)
- `detected_frame_effects: list[str]`
- `detected_is_list: bool | None`
- `disambiguation_source: "single_match" | "disambiguated" | "cheapest_fallback"`
- `disambiguation_confidence: dict[str, float]` — confidence per stage

### UI surfacing (can ship later)

Card-detected debug panel shows which stages fired and their confidence.
Green badge = fully disambiguated; yellow = fell back to cheapest.

### Constants

```python
FRAME_THRESHOLD = 0.60       # confidence required to commit Stage 1
LIST_STAMP_THRESHOLD = 0.55  # confidence required to commit Stage 2
```

---

## Phase 4: Set-Icon Asset Pipeline

One-time build, cached on disk, refreshed when new sets release.

### Steps

1. **`fetch_set_symbols.py`** — pull the `/sets` endpoint from Scryfall,
   read each set's `icon_svg_uri`, download the SVG to
   `card_data/set_symbols/svg/{set_code}.svg`.
2. **Rasterize at multiple scales** (32 px, 48 px, 64 px tall) to
   `card_data/set_symbols/png/{set_code}_{size}.png`. Rationale: set
   icons on cards are small; scale matches the ROI size we'll crop at
   for different frame eras and resolutions.
3. **Monochrome normalization** — convert to black-on-transparent.
   Discard rarity tint. Reason: rarity color is unreliable on foils,
   under damage, or under varied lighting.
4. **Edge template** — run `cv2.Canny` on each rasterized template and
   cache alongside: `card_data/set_symbols/edge/{set_code}_{size}.png`.
5. **Refresh schedule** — hook into the existing Scryfall bulk refresh
   job. New sets release roughly every 6 weeks; weekly or monthly
   refresh is fine.

### Assets to consider

- Scryfall's SVG icons are sometimes stylized (fancy lines, outlines,
  small details that alias at 32 px). Alternatives: Gatherer's set
  symbols, the MSE symbol library, hand-curating hard sets.
- Some set icons are visually very similar (several Masters sets).
  Log pairwise template distances across all sets during this phase
  and flag any pairs with Hamming distance < threshold.

---

## Phase 5: Set-Icon Frame-Era ROI Lookup

Set-icon position on the card depends on **frame era**, which Scryfall
provides in `frame` (and `frame_effects` for treatments).

### Frame → ROI table

| Frame | Position | Notes |
|-------|----------|-------|
| `1993` | No icon | Alpha–4th editions — no set symbol exists. Skip. |
| `1997` | Lower-right, under art, above type line | 6th Ed–Scourge. Black only. |
| `2003` | Right of type line | Mirrodin–M15. Rarity-tinted. |
| `2015` | Right of type line | M15-onward modern frame. |
| `future` | Right of type line | Future Sight frame. Handle as 2003-equivalent initially. |

### `frame_effects` modifiers

- `borderless` — often **no** set symbol. Return `None`.
- `showcase` — position varies per treatment. Start by returning
  `None`; extend per-showcase as we see misses.
- `extendedart` — same location as base frame.
- `inverted`, `colorshifted`, `devoid` — base frame applies.

### API

```python
SET_SYMBOL_ROI: dict[tuple[str, tuple[str, ...]], tuple[float, float, float, float] | None]

def get_symbol_roi(frame: str, frame_effects: list[str] | None) -> tuple[int, int, int, int] | None:
    """Return (x, y, w, h) in pixels on the post-rectification 745x1040
    card image. None means 'no symbol expected — skip icon matching'."""
```

---

## Phase 6: Set-Icon Detection Module

New module `set_icon.py`. Fills in Stage 3 stubbed by Phase 3.

### API

```python
def identify_set_icon(
    card_img: np.ndarray,
    candidate_set_codes: list[str],
    frame: str,
    frame_effects: list[str] | None,
    debug: bool = False,
) -> tuple[str | None, float]:
    """Disambiguate which set among candidates by matching the set icon.
    Returns (best_set_code, confidence). (None, 0.0) when ambiguous or
    no ROI applies."""
```

### Algorithm

1. Look up ROI via `get_symbol_roi(frame, frame_effects)`. If `None`,
   return `(None, 0.0)`.
2. Crop ROI from `card_img`.
3. Preprocess: grayscale → Canny edges.
4. For each `set_code` in candidates:
   - Load cached edge template at closest size
   - Try 2–3 scales (0.9×, 1.0×, 1.1×)
   - Try rotation tolerance (±5°)
   - `cv2.matchTemplate` with `TM_CCOEFF_NORMED`; take max score
5. Return top scorer if `max_score > THRESHOLD` and
   `max_score - second_best > MARGIN`; else `(None, confidence)`.

### Constants (tune from data)

```python
SET_ICON_THRESHOLD = 0.55
SET_ICON_MARGIN = 0.10
SET_ICON_SCALES = [0.9, 1.0, 1.1]
SET_ICON_ROTATIONS = [-5, 0, 5]
```

### Wiring

Replace the Stage 3 stub from Phase 3 with a real call to
`identify_set_icon()`. Filter `same_art` by the winning set code when
confidence exceeds threshold.

---

## Phase 7: Validation + Diagnostics

### Build a labeled test set

- ~50 scans of same-art reprint conflicts (cross-set):
  - Lightning Bolt (M10, M11, 2XM, Strixhaven Mystical Archive, …)
  - Sol Ring (many Commander precons, retro treatments)
  - Counterspell, Swords to Plowshares, Path to Exile
- ~20 scans for The List vs. original, covering cards with big price
  delta between versions
- ~30 scans for frame/border variance:
  - Cards with retro-frame treatments (Double Masters 2022, Brothers'
    War retro artifacts, etc.)
  - Borderless variants vs. regular (recent sets have both)
  - Pre-8ED reprints (ody, tmp, etc.) vs. modern reprints
- Label each with correct `set_code`, `frame`, `frame_effects`, is_list.
- Store as `test_data/printing_disambiguation_labels.json`.

### Test harness

1. `tests/test_printing_disambiguation.py` — parameterized pytest
   running each labeled scan through the full cascade; asserts correct
   printing picked (or correct `None`-fallback on intentionally
   ambiguous cases).
2. `diag_printing.py` — diagnostic script (follows `diag_*.py`
   convention). Input: a scan folder. Output: per-scan candidate list,
   per-stage detection results + confidence, final pick, whether it
   matches a reference.

### Success criteria

- **Disambiguated accuracy**: ≥90% correct on labeled set when cascade
  returns `source="disambiguated"`
- **Ambiguity handling**: when any stage is ambiguous, cascade falls
  through cleanly; no stage should commit to a wrong answer with high
  confidence
- **No regression on unambiguous cards**: when phash returns a single
  confident match, no disambiguation code runs — verify in tests

### Threshold tuning

Once labeled data is in, re-measure and tune:
- `_CONFIDENCE_MARGIN_SCALE` in `frame_detect.py`
- `FRAME_THRESHOLD` in integration
- `LIST_STAMP_THRESHOLD`
- `SET_ICON_THRESHOLD`, `SET_ICON_MARGIN`

---

## Open Questions

1. **Redundancy with frame-dedup fix**: If the foil plan's Priority 1
   (one hash per `(illustration_id, frame)`) ships first, how much of
   Phase 1 becomes redundant? Likely still needed for same-`frame` /
   different-`frame_effects` splits (borderless vs regular, retro
   treatment vs. base frame) since those can share hash space.
2. **SVG rasterization quality** at 32–48 px for set icons — measure
   during Phase 4.
3. **Confusable set pairs** — how many sets have near-identical
   symbols? Does frame-era restriction (only comparing among phash
   candidates) make these non-problems in practice?
4. **Showcase / extendedart coverage** — if rare in typical scan loads,
   punt; if common, build per-treatment ROI and reference entries.
5. **Borderless fallback for set icon** — no icon visible on most
   borderless cards. Is frame detection (Phase 1) enough to
   disambiguate borderless-vs-regular of same set, or do we need
   another signal?
6. **Retro-frame treatment naming** — confirm the exact
   `frame_effects` string Scryfall uses for retro-frame treatments and
   ensure reference vectors cover it.
7. **ROI measurement coordinate space** — all ROIs measured on
   post-rectification 745×1040, not warped raw frames.

---

## Non-Goals / Defer

- Same-set variant disambiguation beyond The List (regular vs
  promo-stamp vs etched within one set) — future follow-up
- Trained classifier / CNN — only build if template matching +
  feature-vector distance falls below 90% on the validation set
- Real-time detection during scanning — the cascade runs only when
  phash returns multiple candidates and each stage is cheap; no
  real-time concerns
- Detecting printing from cards where phash returned zero candidates —
  out of scope; that's a DB-coverage problem
- Pre-8ED same-set disambiguation (no set symbol exists) — out of
  scope; needs a different signal (copyright line OCR)
