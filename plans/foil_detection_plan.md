# Foil Detection & Foil-Aware Identification Plan

## STATUS: REVISED 2026-04-16 — ROOT CAUSE REFRAMED

Prototyping Signal 1 (bright-pixel saturation) and a direct grayscale-phash
test on scan 205 revealed the real bottleneck is **NOT foil distortion** —
it is **DB coverage of the exact printing on the card**.

### Findings from the scan 205 investigation

The plan's motivating example — scan 205 (foil Zombie Infestation) "matching
Pile On at dist 88.7" — turned out to be misdiagnosed:

1. **Current production pipeline** returns "Mind Rot" (kld) at dist 88.67
   — not Pile On. Best wrong match is a dead-confident reject at the
   threshold level.

2. **The actual card is DMR Zombie Infestation**, and the DMR reference
   image was **not in `downloaded_cards/`**. 7 of 8 Zombie Infestation
   printings are missing from the local DB:
   - Present: `plst` (The List, old-frame proxy) — 1 of 8
   - Missing: `ody`, `arc`, `m12`, `pd3`, `c19`, `jmp`, `dmr` — 7 of 8

3. **After downloading the DMR reference** and hashing it, distance dropped
   from 100+ (any wrong candidate) to:
   - RGB phash: **32.67** (just above threshold 30)
   - Grayscale phash: **30.00** (at threshold — this would match)

4. **Grayscale improvement on foils is real but small** (~2–3 points).

5. **DB coverage per set for session 36 is worse than expected**:
   - `mom` (March of the Machine): 85.5% — well covered
   - `dmr` (Dominaria Remastered): **37.0%** — major gap
   - `plst` (The List): 36.0% — expected
   - `cmb1` / `cmb2` (Mystery Booster Playtest): 0.0% — major gap
   - `dpa` (Duels Anthology): 21.2% — major gap

### Revised priority order

| Priority | Action | Expected impact |
|----------|--------|-----------------|
| 1 | **Frame-dedup fix** — download one image per (illustration_id, frame) instead of per illustration_id. Audit found 6,335 multi-frame illustrations and 7,412 missing (art, frame) pairs locally. | Largest — fixes scan 205 directly, likely fixes many other "confident wrong match" cases across the whole scan history |
| 2 | **Rebuild hash DB** on expanded reference set | Follows from (1) |
| 3 | **Add grayscale phash as 4th hash channel** | ~2–3 points better on foils; neutral on non-foils; cheap |
| 4 | **Raise threshold from 30 to ~35** | Accepts borderline-correct foils like scan 205 (dist 30–33) |
| 5 | Foil detection signal (Signal 1 + frame ring) | Only useful if we later want separate handling; not needed to fix scan 205 |
| 6 | Foil-aware ID path | Deferred — the always-on grayscale channel probably closes the gap |

### Frame-dedup bug (root cause, discovered 2026-04-16)

`download_cards.py` dedupes by `illustration_id` — one image per unique art.
But **phash Region A (30, 105, 715, 520) is not art-only** — it spans the
card from 30px below the top (covering title, mana cost, top border art) down
to 520px (covering the type line). A card reprinted with a different frame
has the same art but a different title-box, mana-cost rendering, and top
border — which moves its phash by ~100 bit-distance even though the art is
pixel-identical.

**Example — Zombie Infestation (8 printings, one illustration_id):**

| Set | Frame | Locally present? |
|-----|-------|------------------|
| ody | 1997  | no |
| arc | 2003  | no |
| m12 | 2003  | no |
| pd3 | 2003  | no |
| c19 | 2015  | no |
| jmp | 2015  | no |
| dmr | 2015  | no (was missing — now downloaded manually for test) |
| plst| 1997  | YES (the single "winner" from dedup) |

The scan was a 2015-frame DMR foil. It was matching the 1997-frame plst
reference at distance 100+ (no match). After manually downloading the DMR
reference, distance dropped to **32.67 RGB / 30.00 grayscale** — matching at
threshold. The "foil problem" was 70%+ a missing-frame-reference problem.

**Audit scale (`audit_frame_coverage.py`):**
- 45,190 unique `illustration_id`s in the Scryfall bulk dump
- 6,335 span multiple frames (14% of unique arts)
- Only 9 of those 6,335 are fully covered locally
- **7,412 missing (illustration, frame) pairs** to download:
  - 2,644 in frame 2003
  - 2,526 in frame 2015
  - 1,945 in frame 1997
  - 214 in frame 1993
  - 83 in frame "future"

**Fix path:**
1. `audit_frame_coverage.py` — writes `missing_frame_printings.json`
2. `download_missing_frames.py` — downloads all missing (uses bulk
   `default-cards-*.json` for image URIs; no extra API calls)
3. Rebuild hash DB (`build_hash_db_v3.py`) — hashes expand from ~49k to ~56k
4. Re-run regression on session 36 — measure accuracy delta

**Long-term fix in `download_cards.py`:** change dedup key from
`illustration_id` to `(illustration_id, frame)` so future bulk downloads
don't regenerate this gap.

---

## Original Problem Statement (retained for reference)

Foil cards have an iridescent/rainbow sheen that distorts both the color values
and local luminance in a way that throws off phash and (less severely) DINOv2:

- **phash** hashes a DCT of luminance — foils add specular highlights and
  light-angle-dependent streaks that change the hash significantly
- **DINOv2** is more robust but still trained on non-foil natural images, so
  foil cards fall outside its usual distribution

Original hypothesis: foil distortion alone pushes the scan far from its real
reference. **Rejected** — investigation showed the real reference was simply
missing from the DB. With the correct reference present, foil distortion adds
only 2–3 points vs a non-foil of the same printing.

**Revised goal**: close the DB coverage gap; add grayscale phash as a cheap
always-on channel; measure remaining foil-specific residual before building
any detection-specific code path.

## Scope

**In scope**
- Detect foil-ness of a cropped 745×1040 card image
- Apply a foil-aware identification path when foil is detected
- Measure accuracy impact

**Out of scope**
- Detection algorithm changes (the crop is already good for foils)
- Foil grading / condition scoring (future feature)
- Etched foil vs traditional foil distinction
- Foil-stamped promo cards

---

## Phase 1: Foil Detection

### What makes foils visually distinct

1. **Frame/border sheen** — the card's black frame reflects rainbow light on
   foils. On non-foils it stays near-black.
2. **High hue variance in "flat" regions** — foils have smooth-but-rainbow
   gradients where non-foils are uniform color.
3. **Specular highlights** — bright streaks across the art from angled light.
4. **Saturation pattern mismatch** — foil art has added saturation variance
   that doesn't match the underlying art.

### Proposed signal: Frame hue variance

**Why this region**: The outer black frame of a card is the most reliable
"should be uniform dark color" region across virtually every card layout
(normal, planeswalker, saga, class, battle, adventure all have a black or
near-black outer frame — basic lands being the main exception).

**Measurement**:
1. Sample a ring of pixels 20–40 px inside the card edge (in the outer
   frame region, avoiding the card edge itself)
2. Convert to HSV
3. Filter pixels by value (brightness) — keep only those with V < 100 (the
   "dark frame" pixels, reject light art that happens to touch the ring)
4. For the remaining pixels, compute:
   - `hue_std` — standard deviation of hue (circular std, not linear)
   - `sat_mean` — mean saturation
5. **Foil indicator**:
   - Non-foil: `hue_std` low, `sat_mean` low (dark pixels have no real color)
   - Foil: `hue_std` high, `sat_mean` moderate-to-high (rainbow sheen adds
     coherent color to the black)

**Fallback for full-art / borderless cards (no black frame)**:
- If the frame-region `V < 100` filter returns too few pixels (< 10% of ring),
  fall back to whole-card saturation variance:
  - Sample the full card area
  - Compute saturation entropy (Shannon entropy of S histogram)
  - Compute specular fraction (pixels with V > 240 and S < 30)
  - Foil cards have higher specular fraction and higher saturation entropy

### Calibration

- Build a small labeled set from existing scans:
  - ~20 known foils (the user tagged some as foils during review, or we
    eyeball them — foils are visually obvious)
  - ~100 known non-foils
- Measure distributions of `hue_std` and `sat_mean` for both groups
- Pick a threshold (likely `hue_std > 40°` or so, tunable)
- Report false-positive / false-negative rates at chosen threshold

### API

```python
def detect_foil(card_img, debug=False) -> (bool, float):
    """
    Detect whether a warped 745x1040 card image is foil.
    Returns (is_foil, confidence) where confidence is in [0, 1].
    """
```

---

## Phase 2: Foil-Aware Identification

When foil is detected, the existing phash-on-RGB + DINO pipeline needs to
adapt. Four strategies, in rough order of increasing complexity:

### Strategy A: Grayscale-first phash (primary)

**What**: Precompute a grayscale phash for every card in the DB. When a foil
is scanned, phash the scanned card's grayscale (L channel of LAB, or just
luminance), compare only against the grayscale DB hash.

**Why**: Foils distort color but leave the luminance structure mostly intact
(just adds bright streaks on top of the same underlying image). A grayscale
phash ignores the color distortion.

**Cost**: One more 64-bit hash per card in DB. Small — ~50k × 8 bytes = 400 KB.
One extra hash computation per scan. Cheap.

**Expected gain**: Should cleanly fix foil misidentifications where the
underlying structure is intact.

### Strategy B: DINOv2 as primary for foils

**What**: DINOv2 is a learned feature extractor and is more robust to hue
distortion than phash. For foils, weight DINO more heavily (or use DINO only).

**Why**: DINO features are less color-sensitive than phash (DCT of luminance).
Foils should hurt DINO less than phash.

**Cost**: Already computed in the hybrid pipeline. No extra work.

**Expected gain**: Partial — DINO isn't perfectly foil-invariant either.

### Strategy C: Permissive thresholds for foils

**What**: If foil is detected, raise the match-acceptance threshold by a
fixed amount (e.g. +30) and accept the best match even if distance is higher.

**Why**: Foils will always match at a higher distance than non-foils. If we
know it's a foil, we should stop treating high distance as "no match".

**Cost**: Zero — just a conditional threshold.

**Risk**: Could let in bad matches. Mitigate by requiring DINO and phash to
both rank the same card in top-N (cross-check).

### Strategy D: Specular-masked matching (experimental)

**What**: Detect specular highlights in the foil scan. Mask those regions.
Hash only the non-masked region. Compare against DB hashes using a similar
mask.

**Why**: Specular highlights are the biggest single signal distortion on
foils. If we can remove them, matching quality should recover.

**Cost**: Moderate — need to implement inpainting or masked-phash. Could
use `imagehash` on a masked-crop directly.

**Complexity**: High. Consider last.

### Recommendation

**Start with Strategy A (grayscale phash) + Strategy C (permissive threshold
for foils).** These are low-cost and high-expected-value. If they don't
close the gap, add Strategy B cross-checking. Keep Strategy D in reserve.

### Identification flow (foil-aware)

```
detect_card(frame)                            # current
  -> card_img (745x1040)

detect_foil(card_img)                         # new
  -> (is_foil, confidence)

if is_foil:
    # Foil-aware path
    id_result = identify_card_foil(card_img)  # new wrapper
    # Uses grayscale phash, permissive threshold, DINO cross-check
else:
    # Current path
    id_result = identify_card(card_img)

return id_result + foil_flag
```

---

## Phase 3: DB Extension

Add grayscale-phash channel to the card hash DB (for Strategy A).

**Current DB structure** (from `card_identify.py`):
- Per-card, per-channel phash with `hash_size=16` (256-bit per channel)
- 3 channels (R, G, B) stored as packed uint8 `(3, 32)` arrays
- Packed into `card_hashes_packed.npz` for fast batch popcount/XOR
- Region A (art crop) only (B/C regions dropped after DINOv2 came online)

**Extension**:
1. Add a 4th channel: grayscale (L from LAB, or `cv2.cvtColor(..., BGR2GRAY)`)
   - Same 256-bit phash, computed on the grayscale version of Region A
2. Update `build_hash_db_v3.py` to compute and store the grayscale hash
   alongside R/G/B
3. Store as additional `gray_hashes` packed array in the same `.npz`
4. Update `card_identify.py` to load the grayscale array and expose a
   separate `identify_grayscale()` entry point
5. `card_identify_hybrid.py` gets a foil branch that calls
   `identify_grayscale()` when foil is detected

**Rebuild cost**: ~15-25 minutes for ~51k cards with `ProcessPoolExecutor`
(same as v3 builder originally took).

**Migration**: Additive — add `gray_hashes` array to the `.npz`. Existing
code that only reads R/G/B still works. Old `.npz` files need rebuilding.

---

## Phase 4: Validation

### Test harness

Extend `test_regression_362.py` with a foil-specific breakdown:

1. Run `detect_foil()` on every scan
2. Report accuracy separately for foil vs non-foil groups
3. Show before/after comparison: current accuracy vs foil-aware accuracy
4. Flag any regressions on non-foil cards (foil-aware path should never
   be worse on non-foils since it doesn't run on them)

### Build a labeled foil set

- Manually tag ~30-50 foils across recent sessions using the web UI or a
  simple tagging script
- Store as `foil_labels.json` or similar
- Use as ground truth for `detect_foil()` tuning

### Success criteria

- **Foil detection**: ≥95% accuracy (both directions — few false positives,
  few false negatives) on labeled set
- **Foil identification**: ≥90% correct on the foil group (up from whatever
  current baseline is)
- **Non-foil regression**: Zero — foil-aware path must not touch non-foils

---

## Implementation Order

1. **`detect_foil.py`** — standalone foil-detection module + simple test
   harness on existing scans (no DB changes yet, just measurement)
2. **Tagging tool** — small script or web UI addition to mark cards as foil
   during review (builds the labeled set)
3. **Grayscale phash DB column** — extend build + load + use path
4. **`identify_card_foil()` wrapper** — foil-aware identification
5. **Validation** — regression test with foil breakdown
6. **Wire into web_worker.py** — integrate foil-aware path into live pipeline

---

## Open Questions

1. **Foil threshold**: how aggressive should foil detection be? A false positive
   (detecting foil on a non-foil) routes through the foil path which is fine —
   it uses grayscale phash which works on non-foils too (just slightly less
   discriminative). A false negative (missing a foil) leaves the original
   problem. **Lean toward aggressive detection.**

2. **Etched foils**: Etched foils (from Commander Legends onward) have a
   different visual signature — pattern is more geometric, less rainbow.
   Phase 1 might miss these. Investigate on a case-by-case basis.

3. **Basic lands**: Basic lands often don't have a dark frame (full-art or
   nearly so). The fallback to whole-card saturation stats needs to work
   well for these. Full-art foil lands are common.

4. **DINO-only path**: If grayscale phash alone fixes 80%+ of foil issues,
   we might skip Strategy B. Measure first.

---

## Non-Goals / Defer

- Condition grading from foil surface (scratches, clouding) — future
- Foil vs etched foil classification — future
- Foil detection via lighting-angle change (impractical with fixed camera)
- Storing foil flag in inventory DB for pricing — later, after detection
  is reliable

---

## Separate issue to investigate: regular-card misidentifications

The recent regression test showed two misidentifications on *regular* (non-foil)
cards:

- **Scan 217**: regular Plains, detected upside-down → matched "Melira's Snacks"
  (dist 80)
- **Scan 299**: regular Free from Flesh, detected upside-down → matched
  "Fire Urchin" (dist 82.7, rotated=True)

Both have `rotated=True` behavior in the identifier, meaning the 180° rotation
check *is* being considered — yet another card came out as the best match.

**Hypothesis**: the DB has a matching art printing at a *different* phash than
our scan because of crop-alignment drift after the outward-refinement pass.
The refined polygon is accurate to the outer edge but may shift the art center
by a few pixels vs the Scryfall reference image, which was hashed from the
unshifted canonical 745×1040.

**Diagnosis steps** (not part of foil plan, run independently):
1. Dump top-10 phash candidates + distances for scans 217 and 299 (both
   orientations)
2. Compare distances to the *expected* card (Plains / Free from Flesh) — are
   they just above the threshold, or far off?
3. Pixel-diff the scanned crop vs the expected reference
4. If alignment is the issue: consider a fine-alignment step (template match
   the frame/title band) before hashing
5. If the expected card just isn't in the DB at this art: audit DB coverage
