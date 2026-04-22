# Printing Disambiguation Plan

## STATUS: DRAFT 2026-04-22

Plan captured but not started. Priority: **after** foil detection
(`plans/foil_detection_plan.md`). Foil has bigger pricing impact
(foil-vs-nonfoil delta is typically larger than reprint-variant delta),
so it ships first.

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

## Three disambiguation signals

Each is run only when the phash candidate list disagrees on that
specific axis:

| Signal | What it picks between | Complexity |
|--------|----------------------|-----------|
| **Frame / border** (Phase 6) | `frame` era (1997/2003/2015), `frame_effects` (borderless, retro, showcase, extendedart) | Simplest — huge signal, broad ROIs |
| **The List stamp** (Phase 7) | `plst` reprint vs. original printing of same art | Simple — binary, one template, one ROI |
| **Set icon** (Phases 1–5) | Which `set_code` among multiple same-frame reprints | Hardest — small ROI, hundreds of templates, some confusable pairs |

Ship in order of simplicity (frame → list-stamp → set-icon). Each
further narrows the candidate list; the final pick is whatever survives
all three filters, or the cheapest-printing fallback if any stage is
ambiguous.

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
  phash itself may already distinguish frames, and Phase 6 becomes a
  cross-check rather than a primary signal. Build Phase 6 anyway; it
  catches same-`frame` / different-`frame_effects` cases phash misses.

---

## Phase 1: Set-Icon Asset Pipeline

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
  Log pairwise template distances across all sets during Phase 1 and
  flag any pairs with Hamming distance < threshold.

---

## Phase 2: Frame-Era ROI Lookup (for set icons)

Set-icon position depends on **frame era**, which Scryfall provides in
`frame` (and `frame_effects` for treatments).

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

## Phase 3: Set-Icon Detection Module

New module `set_icon.py`.

### API

```python
def identify_set_icon(
    card_img: np.ndarray,           # post-rectification 745x1040
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

---

## Phase 4: Integration (cascade)

### Hook point

In the identification finalization path (currently
`card_identify_hybrid.py` or wherever phash candidates are resolved to
a single `card_id`):

```python
candidates = phash_match(card_img)

if len(candidates) == 1:
    return candidates[0], source="single_match"

# All candidates sharing illustration_id within close hash distance
same_art = [c for c in candidates if _same_illustration_id(c, candidates[0])]
if len(same_art) < 2:
    return candidates[0], source="single_match"

# Cascade: narrow candidates using each disambiguation signal.
# Each stage reads the current candidate list, applies a filter, and
# returns the filtered list. If a stage is ambiguous, it returns the
# list unchanged.

# Stage 1: frame / border
if _candidates_disagree_on_frame(same_art):
    detected_frame, detected_effects, conf = detect_frame(card_img)
    if conf > FRAME_THRESHOLD:
        same_art = _filter_by_frame(same_art, detected_frame, detected_effects)

# Stage 2: The List stamp
if _candidates_include_list_and_original(same_art):
    is_list, conf = detect_list_stamp(card_img)
    if conf > LIST_STAMP_THRESHOLD:
        same_art = _filter_by_list(same_art, is_list)

# Stage 3: set icon
if _candidates_disagree_on_set(same_art):
    set_codes = [cards_by_id[c.card_id].set_code for c in same_art]
    frame = cards_by_id[same_art[0].card_id].frame
    frame_effects = cards_by_id[same_art[0].card_id].frame_effects
    winning_set, conf = identify_set_icon(card_img, set_codes, frame, frame_effects)
    if winning_set is not None:
        same_art = [c for c in same_art if cards_by_id[c.card_id].set_code == winning_set]

# Whatever survived; if >1 still, fall back to cheapest
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

### UI surfacing

Card-detected debug panel shows which stages fired and their confidence.
Green badge = fully disambiguated; yellow = fell back to cheapest.

---

## Phase 5: Validation

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

---

## Phase 6: Frame / Border Detection

Structurally similar to set-icon detection but uses **feature sampling
in broad ROIs** rather than template matching in a small ROI. The frame
covers most of the card, so signal is huge and detection is easy in
principle.

### What we're picking between

Scryfall `frame` field values: `1993`, `1997`, `2003`, `2015`, `future`.
Scryfall `frame_effects` modifiers that meaningfully change appearance:
`borderless`, `showcase`, `extendedart`, and the retro-frame effect
(modern card rendered in 1997 frame — Scryfall tags this as a frame
effect on the retro treatment cards).

Also `border_color`: `black`, `white`, `borderless`, `silver`, `gold`.

### Gating condition

Run frame detection only when phash candidates disagree on `frame`
**or** `frame_effects` **or** `border_color`. Otherwise skip.

### Signals to sample

1. **Outer-border ring color** — sample a thin ring 3–10 px inside the
   card edge. Mean/median RGB and standard deviation:
   - Uniform dark → black border
   - Uniform light → white border (pre-8ED reprints, some older cards)
   - Uniform yellow/gold → gold border (promo)
   - High variance → borderless (art reaches the edge)
2. **Title-box region** — crop the title-bar ROI (known position on
   rectified card). 1997 has dark beveled frame, 2003 has cleaner
   geometry, 2015 has a flatter gradient style. A second small phash
   on this region discriminates among frames well.
3. **Type-line region** — similar: different frames style the type line
   differently (background color, border, rarity-symbol positioning).
4. **Art-extension check** — for `extendedart`, sample pixels
   **between** the normal art box and the card edge. If those pixels
   look like art continuation rather than frame material, it's
   extendedart.

### Reference vectors

Capture feature vectors once, from one clean reference scan (or
downloaded Scryfall image) per distinct combination:

- `(1993, [], black)`, `(1993, [], white)`
- `(1997, [], black)`, `(1997, [], white)`
- `(2003, [], black)`
- `(2015, [], black)`, `(2015, [], white)`
- `(2015, [borderless], borderless)`
- `(2015, [extendedart], black)`
- `(2015, [showcase], black)` — may need multiple per-showcase vectors
- Retro-frame treatment: `(2015, [<retro effect name>], black)`

Store as `card_data/frame_references.json` with feature vectors per
combination.

### API

```python
def detect_frame(
    card_img: np.ndarray,
    debug: bool = False,
) -> tuple[str, list[str], str, float]:
    """Detect the frame era, frame_effects, and border_color of a
    rectified card image.

    Returns (frame, frame_effects, border_color, confidence).
    Confidence in [0, 1]. Caller checks confidence vs threshold."""
```

### Algorithm

1. Extract feature vector from scan (ring color, title-box phash,
   type-line phash, art-extension check).
2. Compute distance to each reference vector.
3. Pick closest; confidence = `1 - (closest_distance / second_closest_distance)`
   (margin-based confidence).
4. Return best match.

### Why this is simplest

- ROIs are large (full title bar, full ring) — small alignment errors
  don't matter
- Signal differences between frames are **huge** in pixel terms
- Small number of distinct combinations to compare against (~10–15)
- Reuses the rectified card image already in memory

### Constants

```python
FRAME_THRESHOLD = 0.60       # confidence required to commit to a frame
FRAME_MARGIN = 0.15           # margin between top-2 frame matches
```

### Template vs. phash choice

For title-box and type-line regions, prefer a **second phash** on those
crops over template matching. phash is already in the toolchain, and
frames differ in broad structural ways (position of elements, darkness
distribution) that phash captures well.

---

## Phase 7: "The List" Stamp Detection

The List (`plst` set code) reprints cards with the same art as the
original printing but adds a **planeswalker-symbol stamp in the
bottom-left corner**. Binary detection: stamp present → `plst`, stamp
absent → original printing.

### Gating condition

Run List-stamp detection only when phash candidates include **both** a
`plst` printing and at least one non-`plst` printing of the same
`illustration_id`.

### ROI

Bottom-left corner of the rectified card. Approximate pixel coordinates
on the 745×1040 image (tune from known List scans during
implementation):

```python
THE_LIST_STAMP_ROI = (30, 960, 80, 70)  # (x, y, w, h)
```

### Algorithm

1. Crop `THE_LIST_STAMP_ROI` from rectified card.
2. Preprocess: grayscale → Canny edges.
3. Single template match against cached List-stamp template.
4. Return `(is_list: bool, confidence: float)`.

### API

```python
def detect_list_stamp(
    card_img: np.ndarray,
    debug: bool = False,
) -> tuple[bool, float]:
    """Detect whether the scan has The List planeswalker stamp.
    Returns (is_list, confidence)."""
```

### Template asset

Capture from 2–3 known-good List scans, average, clean up, store at
`card_data/stamps/the_list.png` + Canny edge version. One-time manual
step.

### Scope fences

- Don't detect promo stamps, pre-release stamps, or other corner
  stamps — future work.
- Don't try to detect older "List" variants before the planeswalker
  stamp was added — fall back to cheapest-printing logic for them.

---

## Implementation Order

Recommended order, simplest signal first:

1. **Phase 6: frame/border detection** — broadest signal, easiest
   implementation. Also the most frequently applicable (many reprints
   differ on frame). Use the frame-dedup audit from
   `plans/foil_detection_plan.md` to generate labeled scans.
2. **Phase 7: List stamp** — binary, one template, one ROI. Simple
   validation. Delivers immediate value (The List vs. original has big
   price delta).
3. **Phase 4: integration scaffolding** — wire the cascade into the
   identification flow using just Phases 6 + 7 initially. Proves the
   integration path works end-to-end.
4. **Phase 1–3: set-icon module** — the hardest piece (many templates,
   small ROI, confusable pairs). Build once scaffolding is proven.
5. **Phase 5: full validation + `diag_printing.py`** — tune thresholds
   across the cascade.
6. **UI surfacing** — badges, debug panel. Ship last.

Each step is independently testable and each narrows the "wrong
printing" failure mode further.

---

## Open Questions

1. **Redundancy with frame-dedup fix**: If the foil plan's Priority 1
   (one hash per `(illustration_id, frame)`) ships first, how much of
   Phase 6 becomes redundant? Likely still needed for same-`frame` /
   different-`frame_effects` splits (borderless vs regular, retro
   treatment vs. base frame) since those can share hash space.
2. **SVG rasterization quality** at 32–48 px for set icons — measure
   during Phase 1.
3. **Confusable set pairs** — how many sets have near-identical
   symbols? Does frame-era restriction (only comparing among phash
   candidates) make these non-problems in practice?
4. **Showcase / extendedart coverage** — if rare in typical scan loads,
   punt; if common, build per-treatment ROI and reference entries.
5. **Borderless fallback for set icon** — no icon visible on most
   borderless cards. Is frame detection (Phase 6) enough to
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
