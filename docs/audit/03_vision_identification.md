# 03 — Vision & Identification Audit

Branch: `docs/audit-and-backlog`. Scope: capture → detect/warp → identify → printing disambiguation → foil, plus the reference-data builders and the separate `D:\Card_Sorter\card_art_id_handoff` repo. Line numbers are from the working tree as of this audit. No code was changed.

---

## 0. TL;DR

- **Live path:** `web_worker._cmd_detect_and_sort` → `card_detect.detect_card_with_corners` → foil "B" frame capture (saved, **never scored**) → background thread: `card_identify_hybrid.is_card_back` (phash) → `card_identify_hybrid.identify_card` (phash **and** DINOv2 ViT-B/14 on every card) → cheapest-printing remap → `printing_disambiguation.disambiguate_printing` (frame → List stamp → set icon) → `foil_detect.detect_foil` (single frame compared with the Scryfall PNG).
- **Dead or legacy:** `detection.py` (only `save_staging_bg` and `save_staging_roi` are live), `hashing.py`, `ocr.py`, `layout_signatures.py`, `create_card_hashes_v2.py`, `16bit_rgb_create_card_hashes.py`, `frame_era_classifier.py`, and `frame_template_matcher.py` (bench tools only). `card_identify.py` and `card_identify_v2.py` are live only as the two halves of the hybrid.
- **Top issues:**
  1. The foil pair capture adds about 1 s of serial motion and settle time to every card, and nothing uses its output.
  2. The foil model's Level-5 labels for session 59 come from `scan_history.is_foil`, which the detector writes itself. That makes the training set circular, and the reported metrics are in-sample.
  3. Recalibrating the staging ROI never invalidates `card_detect`'s cached ROI.
  4. `set_icon` silently drops candidates that have no template, so it can commit to the wrong set.
  5. `foil_detect._fetch_reference` can `json.load` a 540 MB Scryfall bulk file inside the per-card ID thread.
  6. DINOv2 runs two ViT-B forward passes on every card, even when phash is already confident.
  7. Both identifiers sort about 62 K Python tuples per call.
- **Up-camera redesign:** the Scryfall-derived DBs (phash, embeddings, frame signatures, set-icon templates) survive as long as the image is not mirrored. Everything scan-derived or scan-tuned must be redone: staging ROI and background, all thresholds, foil weights, the frame template matcher, and the regression sets. Current foil detection cannot carry over, because its weights encode the old LED geometry. I recommend a controlled multi-illumination capture (2–4 strobed LEDs, optionally cross-polarized) with the card stationary on the head. The Scryfall `finishes` field should be used as a free prior right away.

---

## 1. Live pipeline end to end

### 1.1 Entry points (traced from `web_server.py` / `web_worker.py`)

| Step | Code | Notes |
|---|---|---|
| Preload at boot | `web_server.py:5236-5242` imports `cards`, `card_identify_hybrid`, `card_detect` | Importing the hybrid loads the phash npz, `torch.hub` DINOv2 and `card_embeddings.npz` (185 MB). If preload fails, the first card pays for it. |
| Sort cycle | `web_worker.py:1409` `_cmd_detect_and_sort` | Imports at `:1423-1436`. |
| Test-scan cycle | `web_worker.py:4071-4075` | Same hybrid identifier. It uses `detect_card` and does not run disambiguation or foil detection. |
| Staging calibration | `web_worker.py:1295+` `_capture_staging_background` → `detection.save_staging_bg/save_staging_roi`; `:1380` `set_staging_roi` | The only live use of `detection.py`. |

### 1.2 Per-card sequence (`_cmd_detect_and_sort`)

1. **Pick and stage:** `pick_from_position` → `drop_on_staging` (`web_worker.py:1483-1500`).
2. **Capture:** `move_to_camera_position`, then `sleep(0.5)`, `flush_buffer`, `sleep(0.3)` (`:1503-1507`). On the first card of a session the worker blocks for up to 60 s on a user "Lock Focus" prompt (`:1518-1531`). Then `camera.get_sharp_frame(min_sharpness=50, max_wait=2.0, settle_frames=3)` (`:1536`). About 0.8 s of fixed sleeps plus 0–2 s of sharpness wait before any vision work starts.
3. **Detect and warp:** `card_detect.detect_card_with_corners(frame)` (`:1555`), which calls `_find_card_contour` (`card_detect.py:168`). It computes Canny 30/90 on gray, B, G and R, ORs B, G and R, then runs a 2-pass morph close (`_find_best_card_in_edges`, `:295`). Filters: area 0.28–0.60 × staging-ROI area, centre inside the ROI ±100 px, solidity ≥0.65, aspect within 35 %, rectangularity ≥0.70, `approxPolyDP` to 4 points. If the OR path fails it falls back to one channel at a time. Then `_refine_polygon_outward` (`:439`) and `_perspective_warp` to 745×1040 (`:585`). If nothing is found, the card retries from source up to `_max_no_detect_retries` (`:1556-1583`).
4. **Foil pair (B frame):** `_capture_foil_pair(corners, dx_mm=5.0)` (`:1598`, body `:2277-2330`). It moves X +5 mm, waits for completion, `sleep(0.25)`, flushes, `sleep(0.2)`, then `get_sharp_frame(max_wait=1.5)` and warps with shifted corners. The crop is **only stashed and saved to disk** (`_flush_foil_b_crop`, `:2332`). The live `detect_foil` never reads it. Its only consumers are the offline tools `probe_foil_pairs.py` and `generate_foil_review.py`. This runs synchronously on the motion thread, before identification starts.
5. **Identify (background thread, overlapped with `pick_from_staging`)** (`:1606-1790`):
   - `is_card_back(img)` is re-exported from `card_identify.py:243`: auto-levels, then Region A phash in 2 orientations, compared against the `_card_back` hash (threshold 100).
   - `identify_card(img, threshold=120)`, the hybrid (`card_identify_hybrid.py:102`):
     - phash (`card_identify.py:270`): auto-levels, then R/G/B phash at 256 bits on Region A `(30,105,715,520)` at 0° and 180°, packed XOR+popcount against `card_hashes_packed.npz` (N≈62 K), then a full Python sort.
     - DINOv2 (`card_identify_v2.py:368`): art crop → 518² → ViT-B/14 CLS, 768-d, run twice (0°/180°), a matmul against `card_embeddings.npz`, then a full Python sort.
     - Decision rules (`card_identify_hybrid.py:129-161`): names agree → phash; phash ≤82 → phash; phash ≤85 with gap ≥5 → phash; otherwise DINO, with a **synthetic distance** `(1-sim)*200`.
     - `_remap_to_cheapest` (`:169`) rewrites the winner to the cheapest nonfoil printing with the same art.
   - Worker filters `all_results` to paper and non-`EXCLUDED_SETS` (`:1664-1672`). It accepts if `top_dist <= PHASH_DISTANCE_THRESHOLD (120)` (`config.py:29`).
   - **Printing disambiguation** (`:1697-1722`) → `printing_disambiguation.disambiguate_printing(oriented, top_id)` (`printing_disambiguation.py:63`). It pulls every English same-illustration printing from `cards`, then runs `_run_cascade` (`:119`):
     - Stage 1: `frame_detect.detect_frame` (border-strip phash against `frame_signatures.json`) commits at confidence ≥0.60.
     - Stage 2: `list_stamp.detect_list_stamp`, tight ROI `(0,968,35,30)`. It commits "is List" at ≥0.35 and "is not List" below 0.175.
     - Stage 3: `set_icon.identify_set_icon` (edge+gray `matchTemplate` on the frame-era ROI) commits at ≥0.40 with margin ≥0.05.
     - With more than one survivor, `_cheapest_among` picks.
   - **Foil** (`:1733-1745`): `foil_detect.detect_foil(oriented, card_id=top_id)` (`foil_detect.py:402`). It loads the reference PNG (direct file, else the `printings_map` representative, else a **network download**), picks the DFC face by phash, and scores a 5-feature logistic model at threshold 0.75.
   - Identity gate: `hash_distance >= IDENTITY_LOW_CONFIDENCE_DISTANCE (90)` routes to the fallback bin (`:1845-1859`).
6. **Route and drop:** the sort config gets `is_foil` and `printing_disambiguated` injected (`:1826-1832`).

### 1.3 What is dead or experimental

| File | Status | Evidence |
|---|---|---|
| `card_identify_hybrid.py` | **LIVE** (production identifier) | `web_worker.py:1429, 4074`; `web_server.py:5239` |
| `card_identify.py` | LIVE as the phash half of the hybrid | `card_identify_hybrid.py:30` |
| `card_identify_v2.py` | LIVE as the DINOv2 half of the hybrid | `card_identify_hybrid.py:37` |
| `card_detect.py` | **LIVE** (detector and warp) | `web_worker.py:1423, 2288, 4072` |
| `printing_disambiguation.py`, `frame_detect.py`, `list_stamp.py`, `set_icon.py`, `set_symbol_roi.py` | LIVE (cascade) | `web_worker.py:1697` |
| `foil_detect.py` | LIVE | `web_worker.py:1430` |
| `card_lookup.py` | LIVE, but **not in the vision path**: collection/query enrichment | `collection_db.py:23` (top-level import), `web_server.py:2086` |
| `detection.py` | DEPRECATED, apart from `save_staging_bg`, `save_staging_roi`, `load_staging_bg` | header at `detection.py:1-8`; `web_worker.py:1307, 1388` |
| `hashing.py` | DEAD in production. The only production reference is inside `_save_hash_diagnostics`, **after an unconditional `return`** (`web_worker.py:2356-2360`). Importing it loads the 18 MB v1 JSON. | header at `hashing.py:1-7` |
| `ocr.py` | DEAD (only `test_ocr*.py` / `test_10_cards.py`). Requires pytesseract, which is not in requirements.txt. | — |
| `frame_era_classifier.py`, `frame_template_matcher.py` | EXPERIMENTAL (bench and eval scripts only) | importers: `bench_cascade_stages.py`, `eval_frame_vs_manual.py`, `frame_label_tool.py`, ... |
| `layout_signatures.py` | DEAD one-off script with hardcoded `C:\Users\Jon\Downloads\default-cards-20241206100658.json` (`:10`) | no importers |
| `create_card_hashes_v2.py`, `16bit_rgb_create_card_hashes.py` | DEAD builders for `card_hashes_v2.json` / `card_hashes.json` | runtime reads only `card_hashes_v3.json` / `card_hashes_packed.npz` |
| `build_hash_db_v3.py` | LIVE builder (phash DB) | → `card_hashes_v3.json` → auto-packed to npz by `card_identify._load_db` |
| `build_embedding_db.py` | LIVE builder (DINO DB) | → `card_embeddings.npz` |
| `build_png_roi_templates.py` | LIVE builder (set-icon templates) | → `card_data/set_symbols/roi_templates/` |
| `download_cards.py` | LIVE builder (reference PNGs + `printings_map.json`) | — |

---

## 2. Per-file summary

- **`card_detect.py` (637 lines):** staging-ROI-gated contour detector. It uses multi-channel Canny (30/90), 3×3 rect+ellipse close at iter 1 then 3, and area/solidity/aspect/rectangularity filters. `approxPolyDP` falls back to `minAreaRect`. `_refine_polygon_outward` snaps from the inner frame to the outer card edge, then `_perspective_warp` produces 745×1040. `warp_with_offset` and `get_staging_px_per_mm` exist for the foil pair. The ROI is cached at import (`:71`).
- **`card_identify.py` (323):** phash identifier. It uses `card_hashes_packed.npz` (N×3×32 uint8), with a 75 s JSON fallback that rewrites the npz. Query-only auto-levels, Region A, 2 orientations, LUT popcount. Also provides `is_card_back`.
- **`card_identify_v2.py` (245):** DINOv2 identifier. It calls `torch.hub.load('facebookresearch/dinov2','dinov2_vitb14')` at import, which needs the hub cache or network. Art crop, 518², cosine matmul. The docstring says ViT-S/384-d; the code uses ViT-B/768-d. The `is_card_back` here is unused (the hybrid re-exports the phash one).
- **`card_identify_hybrid.py` (174):** decision fusion plus cheapest-printing remap. It claims "100 % on 362-scan regression set" and ~320 ms/card.
- **`hashing.py` (382):** v1 top-crop, CLAHE-based identifier. Dead.
- **`detection.py` (1588):** old detector family (no-bg, bg-sub, colour-seg, heavy-blur, ArUco bbox, CLI ROI tools, layout detection, colour correction, border type, orientation). Only the staging bg/ROI save/load is still live.
- **`foil_detect.py` (624):** reference-relative bright-pixel statistics (bright_frac, mean S, cluster count, S std, Laplacian energy) in a logistic model. Loads references directly, via the printings_map rep, or by on-demand download. Picks the DFC back face by phash.
- **`printing_disambiguation.py` (288):** the 3-stage cascade and `_cheapest_among`. Module header still says Stage 3 is "TODO … stubbed" (`:12`), which is stale.
- **`frame_detect.py` (227):** 64-bit phash of the top, left and bottom 50 px strips against `frame_signatures.json` per (frame, frame_effects). Confidence = margin/30.
- **`set_symbol_roi.py` (136):** frame-era → symbol ROI table. 1997 = `(600,495,110,160)`, 2003 = `(555,580,140,55)`, 2015 = `(650,590,50,55)`. Borderless and showcase → None.
- **`set_icon.py` (213):** Canny+gray `matchTemplate` over 3 scales × 3 rotations against averaged Scryfall-crop templates. Ensemble = edge + 0.35·gray. Threshold 0.40 and margin 0.05; the plan specified 0.55/0.10. The validation figure quoted in the code is **73.3 %**.
- **`frame_era_classifier.py` (166):** scalar-feature decision tree (retro/2003/2015/borderless × border colour). Bench only.
- **`frame_template_matcher.py` (230):** bottom-half averaged templates built **from real scans** (`from_scan_logs`). Bench only.
- **`layout_signatures.py` (154):** one-off layout feature dump. Dead.
- **`ocr.py` (255):** Tesseract title/collector OCR with multiple preprocessing strategies. Dead.
- **`card_lookup.py` (131):** loads the **entire** `CARDS_JSON_PATH` bulk file at import (a second full copy after `cards.py`). Provides name and set/collector indices plus fuzzy name lookup.
- **`build_hash_db_v3.py` (243):** hashes regions A, B and C with both phash and dhash (**18 hashes per card; the runtime uses 3**). It applies the exclusion filter.
- **`build_embedding_db.py` (211):** batched DINOv2 over **all** `downloaded_cards/*.png`, with **no exclusion filter** (asymmetric with the phash DB).
- **`create_card_hashes_v2.py` (284), `16bit_rgb_create_card_hashes.py` (101):** legacy builders.
- **`build_png_roi_templates.py` (229):** builds set-icon templates only for sets that appear in `scan_logs` (`sets_in_scan_logs`, `:50`). It downloads PNGs into `tmp/card_png_cache` and excludes `plst`.
- **`download_cards.py` (443):** fetches Scryfall bulk, dedups by (illustration_id, frame), lifts DFC faces as `{id}__back`, writes `printings_map.json`, and downloads PNGs at 10 req/s. It has an interactive `input()` prompt.
- **`card_art_id_handoff/` (separate git repo, 1 commit):** a clean-room, one-way export of the sorter's **phash path only**, intended for a React/TypeScript phone app. It contains:
  - `SPEC.md`, a Python reference (`phash.py` from primitives, bit-identical to imagehash; `dbformat.py`, a portable `cardhashes.bin`; `identify.py`, which uses `argsort` top-N; and `detect.py`, which is `card_detect` with the staging-ROI coupling removed and area as a fraction of the frame).
  - Golden vectors, and a TS scaffold (`backend.ts` stub still open).
  
  Nothing in the sorter imports it. It relates to the sorter in two ways: (a) its identifier is the same algorithm and thresholds as `card_identify.py`, so it is a clean, tested spec of our phash path; (b) its **generic, ROI-free detector** is a better starting point for the up-camera than `card_detect.py`. Any change to Region A, auto-levels, or `HASH_SIZE` in the sorter breaks parity with this package.

---

## 3. Bugs, accuracy risks, performance hot spots

### 3.1 Bugs

| # | Severity | Location | Issue |
|---|---|---|---|
| B1 | High | `detection.py:701-707` vs `card_detect.py:39-71` | `save_staging_roi` invalidates only `detection.py`'s cache. `card_detect.invalidate_staging_roi()` (`:64`) has **no callers**. After an in-session ROI recalibration (`web_worker.py:1380-1391`), detection keeps the old area/location gates and `get_staging_px_per_mm` until a restart. |
| B2 | High (accuracy) | `set_icon.py:190-197` + `build_png_roi_templates.py:50` | Candidates with no template are **silently skipped**, and templates exist only for sets previously seen in `scan_logs`. If the true set has no template and one other candidate's template scores ≥0.40, there is no runner-up to fail the margin check, so it **commits the wrong printing**. The result is labelled `icon_disambiguated`, and the worker then trusts that printing's price (`printing_disambiguated=True`). |
| B3 | High (latency/memory) | `foil_detect.py:98-145, 363-366` | When a reference PNG is missing, `_build_image_url_index` `json.load`s the newest `default-cards-*.json` (~540 MB, several GB as Python objects) **inside the per-card ID thread**, then does a blocking HTTP download (15 s timeout). The first miss stalls a card for tens of seconds. It also picks the newest file by glob, not `config.CARDS_JSON_PATH`. |
| B4 | Med | `web_worker.py:1598, 2277-2330` | The foil B-frame capture costs about 0.25 + 0.2 s of sleeps, plus the move and up to 1.5 s of sharp-frame wait, **on every card**. The result is never used by `detect_foil`. It is pure data collection in the hot path. |
| B5 | Med (accuracy) | `card_identify_hybrid.py:148, 157` + `web_worker.py:1677` | When DINO wins, `all_results[0]` carries a **synthetic** distance `(1-sim)*200`. This then feeds the phash-calibrated 120 accept threshold and the 90 low-confidence gate. The two scales are not comparable (sim 0.55 → 90 → low-confidence; sim 0.4, DINO's own floor, → 120 → accepted). If the DINO pick is later filtered out (excluded set or non-paper, `:1664-1672`), the worker silently falls back to phash's #2 at phash distance. |
| B6 | Med | `card_identify_hybrid.py:86-99` | `_names_match` treats substring or 15-character prefix as agreement ("Island" ⊂ "Island Sanctuary"; any pair sharing a 15-character prefix). This triggers Case 1 (trust phash) on disagreement. |
| B7 | Med | `foil_detect.py` (reference) | The score compares the scan with the reference of the **final disambiguated or cheapest printing**. If disambiguation picked the wrong frame, the reference art or frame differs, and the brightness deltas look foil-like (the same failure mode as the DFC bug the code already fixes). |
| B8 | Low | `build_embedding_db.py:148-160` | The batch flush condition `i == n-1` sits after a `continue` on load failure. If the **last** image fails to load, the final partial batch is never flushed, and those rows keep uninitialised `np.empty` values. |
| B9 | Low | `card_identify.py:90-108` | npz freshness is `mtime(npz) >= mtime(json)`. Touching or re-checking-out `card_hashes_v3.json` forces a 75 s JSON parse and an npz rewrite **at import in the server**. |
| B10 | Low | `download_cards.py:441` | The "Next steps" message tells the operator to run `16bit_rgb_create_card_hashes.py` (the v1 builder). It should say `build_hash_db_v3.py` + `build_embedding_db.py` + `build_png_roi_templates.py`. |
| B11 | Low | `download_cards.py:19, 45, 363` | No User-Agent or Accept headers (Scryfall API policy requires them), no retry, and `input()` blocks automation. |
| B12 | Low | `card_identify_v2.py:1-12, 180, 245`; `build_embedding_db.py:5-15` | Docs say ViT-S/384-d; the code is ViT-B/768-d. `foil_detect.py:420` documents confidence "in ~[-1,+1]", but it is an unbounded logit. |
| B13 | Low | `_perspective_warp` (`card_detect.py:599-616`) | Corner ordering by y then x breaks for quads rotated about 45°, and the portrait/landscape pick allows 90° errors. Identification only tries 0° and 180°. This is fine for the current guided staging, but matters for a new rig. |
| B14 | Low | `printing_disambiguation.py:12` | Stale "TODO Phase 6 — stubbed" header; Stage 3 is implemented. |

### 3.2 Accuracy risks

- **Foil model validity:**
  - Session 59 (1017 scans, 62 % of training rows) is labelled from `scan_history.is_foil` (`_foil_tune.py:204-225`). That is the value the **live detector wrote**, unless every row was hand-reviewed. Training on your own predictions inflates agreement.
  - All reported precision and recall figures (`plans/foil_retune_2026-05-07.md`) are **in-sample threshold sweeps**, with no held-out split or cross-validation.
  - Training prevalence is about 17 % foil. At a realistic bulk-collection prevalence (a few %), precision at the same threshold will be much lower than 92 %.
  - Known structural failures: pre-2010 ("old physics") foils, etched foils, and anything whose reference render is not comparable in brightness. Examples: old-border cream stock (Giant Growth 6ED FP), and auto-exposure drift.
- **Set icon:** 73 % validation accuracy at a threshold looser than the plan (0.40/0.05 against 0.55/0.10). Phase 7 (labelled validation) was never done. See B2.
- **List stamp:** the "confident no" branch (<0.175) filters out `plst` candidates almost always. The threshold was tuned on Scryfall PNGs, not scans. The ROI is a 35×30 px corner patch, so it is very sensitive to warp error at the bottom-left corner.
- **Frame detect:** the margin scale of 30 is "empirically chosen, tune from diag data". It has not been re-tuned on scans.
- **DB asymmetry:** the phash DB excludes oversized, art-series and memorabilia cards; the embedding DB does not. DINO can pick an art-series card, which the worker's filter only partly catches (it filters `EXCLUDED_SETS` and `paper`).
- **Hybrid regression claim:** "100 %/0 wrong on 362 scans" is from a single session under the old lighting. Thresholds 82, 85 and 5 were fit to that set.
- **Cheapest remap before disambiguation** is harmless (same illustration group), but it means `identify_card`'s own printing is discarded. Disambiguation is the only printing signal.

### 3.3 Performance hot spots (per card, estimates from code and comments; measure before optimising)

| Stage | Cost driver | Location |
|---|---|---|
| Fixed sleeps plus sharp-frame wait | 0.8 s fixed + 0–2 s | `web_worker.py:1504-1537` |
| Foil B frame (serial, motion thread) | ~0.5 s sleeps + move + 0–1.5 s | `web_worker.py:2305-2313` |
| Detection | 4× Canny + 2–10 `morphologyEx` + contours; refine = 4×40×40 Python pixel loop | `card_detect.py:204-290, 508-520` |
| Card back | auto-levels + 6 phash (repeated inside identify) | `card_identify.py:243-263` |
| phash 1:N | 2× (N×3×32 XOR → LUT gather to uint32, **~24 MB temporary each**) + `tolist()` + **Python sort of ~62 K tuples** with canonicalise per element | `card_identify.py:230-232, 313-316` |
| DINOv2 | **2 ViT-B/14 forward passes at 518² (1369 tokens)** per card, always, even when phash is confident. On CPU this is seconds; on GPU tens of ms. | `card_identify_v2.py:387-393`; hybrid `:122` |
| DINO 1:N | 2× (N×768) matmul + **Python sort of ~62 K tuples** | `card_identify_v2.py:328, 408-412` |
| Disambiguation | small: 3 phash + ≤(#sets × 18) `matchTemplate` | — |
| Foil | full-image HSV/Laplacian/connected components on 2 images + PNG decode (1.5 MB) + optional 3 phash; **cold path: 540 MB JSON + HTTP** | `foil_detect.py:216-300` |
| Model/DB load | `torch.hub` (hub cache/network), 185 MB embeddings, 8 MB phash npz; `cards.py` + `card_lookup.py` each parse the full bulk JSON (~540 MB), and `foil_detect` may add a third copy | import time |

---

## 4. Optimisations and consolidation

**Identifier versions — keep one path:**
1. Keep `card_identify.py` (phash) as the primary and `card_identify_v2.py` (DINO) as the fallback, fused in the hybrid. **Change the hybrid to be lazy:** run DINO only when phash fails the ≤82 / gap rules. Most cards are phash-confident, which saves 2 ViT passes per card. Also run DINO on the orientation phash picked, not both (1 pass).
2. Replace the full Python sorts with `np.argpartition(dists, K)[:K]` + `argsort` for K≈50. Canonicalise only those K. The handoff `identify.py:161` already does this. Consumers only read `[:5]` or `[1]`.
3. Compute `_auto_levels` and the PIL conversion once, and share them between `is_card_back` and `identify_card`. Use a byte-wise popcount on `np.unpackbits`, or `np.bitwise_count` (NumPy ≥2.0), to avoid the 24 MB uint32 temporary.
4. Adopt the handoff package's `phash.py`, `dbformat.py` and `identify.py` as the sorter's phash core (one spec, golden vectors, no imagehash dependency at runtime). Keep a parity test between the two repos.
5. Delete or archive `hashing.py`, the dead `_save_hash_diagnostics` body, `ocr.py`, `layout_signatures.py`, `create_card_hashes_v2.py`, `16bit_rgb_create_card_hashes.py`, and the deprecated parts of `detection.py`. Move `save/load_staging_*` into `card_detect.py` so there is one ROI cache (fixes B1).
6. Decide the fate of `frame_era_classifier.py` and `frame_template_matcher.py`: promote one into the cascade, or archive both.

**Reference data files (repo root, ~5.3 GB of JSON, ~190 MB npz):**
- `card_hashes.json` (19 MB, v1), `card_hashes_smaller.json` (30 MB), `card_hashes_v2.json` (45 MB): **dead**. Delete them.
- `card_hashes_v3.json` (82 MB) is only the source for `card_hashes_packed.npz` (8 MB) and holds 6× the needed hashes. Change `build_hash_db_v3.py` to emit only Region A phash, write the npz (or the handoff `cardhashes.bin`) directly, and drop the JSON and the mtime race (B9).
- Nine `default-cards-*.json` Scryfall dumps (~4.7 GB); only `default-cards-20260521210656.json` is referenced (`config.py:16`). Prune the rest and add a retention rule to the downloader.
- `printings_map.json` (47 MB), `frame_signatures.json` (8.6 MB) and `missing_frame_printings.json` (1.5 MB) are **git-tracked**. Consider Git LFS, or regenerating them. The `card_hashes*` and `card_embeddings` files are already gitignored.
- One loader: `cards.py`, `card_lookup.py` and `foil_detect` should share one parsed bulk index. `card_lookup` should index from `cards.CARDS_DATA`, and foil should use `cards` for image URIs.
- Add a manifest (builder version, source bulk-file name, card count, hash params) next to each DB, and assert consistency at load: same ID set across the phash DB, embeddings and `printings_map`.

**Latency:**
- Take the foil B capture out of the hot path now (B4). Make it opt-in "data collection mode".
- Replace the fixed sleeps with the sharpness gate alone.
- Precompute the image-URL index offline, or never fetch in the hot path: return `no_reference` and queue a background fetch.

---

## 5. Impact of the up-camera redesign

**New geometry:** the card is loaded face-down and held by a suction head over a fixed upward-facing camera, then imaged from below while held.

### 5.1 Detection and warp

- **Mirroring:** a camera that looks directly at the printed face produces a **non-mirrored** image. Mirroring only appears if the optical path contains a mirror or prism, or if the driver/UVC flip is set. The card's orientation relative to the frame is flipped compared with the old top-down view (a rotation, not a mirror), so the 0°/180° search still covers it, **provided the card stays long-axis aligned**. Recommended:
  - add `CAMERA_MIRROR` / `CAMERA_ROTATE` config applied right after capture;
  - add a calibration self-test that images a known card and checks that phash distance at 0°/180° is below the threshold **and** lower than the mirrored variant. Every downstream ROI (Region A, set-icon ROI, List stamp at the bottom-left corner, frame strips) is chirality-sensitive, and a mirror breaks all of them silently.
- **Background:** behind the card are the head, the suction cup, the gantry and the machine interior, not a uniform staging platform. Black-bordered cards against a dark machine interior will defeat Canny. Options, in order of preference:
  - **Fixed-pose warp:** the head presents the card at a mechanically repeatable position, so calibrate a camera→card homography once (ArUco or a calibration card) and warp a fixed quad. Then do a small `_refine_polygon_outward`-style edge snap or ECC alignment for residual offset. No contour search, and most `card_detect` failure modes disappear.
  - Put a matte, high-contrast backdrop (white or chroma-coloured) on the head, larger than the card, so edges always have contrast.
  - Fall back to the handoff's generic `detect.py` (area fractions of the frame, no staging ROI).
  
  `staging_roi.json`, `staging_bg_ref.png`, the 0.28–0.60 ROI-area gates, and `get_staging_px_per_mm` all become meaningless. Replace them with a "presentation ROI + homography" calibration.
- **Sag and non-planarity:** a single central suction cup lets the card edges droop toward the camera. The perspective warp assumes a plane, so sag produces barrel-like magnification at the edges. That is irrelevant for 64×64 phash on the art window, but it matters for:
  - the List stamp (35×30 px corner ROI);
  - the set-icon ROI near the right edge;
  - the frame border strips (`frame_detect`, 50 px edges);
  - edge-based detection.
  
  Mitigations: a flat backing plate or multi-cup head; a longer working distance (narrower FOV, so less magnification change per mm of sag); optionally a 4×2 mesh warp from edge-line fitting instead of a single homography.
- **Focus and exposure:** the working distance is fixed, so use fixed focus and fixed exposure/white balance from a calibration. Drop the first-card "Lock Focus" prompt (`web_worker.py:1518`) and auto-exposure settle sleeps. Fixed exposure is also a **prerequisite for any photometric foil method**.
- **Lighting from below (same side as the camera):**
  - Specular glare from the card's gloss will reflect straight into the lens unless the lights sit well off-axis (≥30–45° to the camera axis) or are cross-polarised.
  - Diffuse ring or dome lighting plus a lens analyser crossed with a polariser on the LEDs gives a glare-free image, which is best for ID. It also suppresses foil sheen, so foil needs its own exposure (§5.3).
- **Motion:** if the card is imaged while the head moves over the camera, use a strobe or a short exposure (global shutter preferred). Rolling shutter plus motion skews the quad.

### 5.2 Calibration and reference data to regenerate

| Artifact | Regenerate? | Why |
|---|---|---|
| `card_hashes_packed.npz`, `card_embeddings.npz`, `frame_signatures.json`, set-icon `roi_templates/`, `downloaded_cards/`, `printings_map.json` | **No**, if the image is un-mirrored and 745×1040 canonical | Built from Scryfall PNGs, so independent of the camera. |
| `staging_roi.json`, `staging_bg_ref.png`, `staging_calibration.json`, `bounding_box.json` | **Replace** | New presentation geometry, homography and backdrop. |
| Card-back reference / `CARD_BACK_THRESHOLD=100` | Re-tune | New lighting and contrast (the reference image itself is fine). |
| phash thresholds 120 / 90 (`config.py:29,44`), hybrid 82 / 85 / gap 5 (`card_identify_hybrid.py:47-55`), DINO 0.4 | **Re-tune** on a new labelled capture set | Calibrated on 362 old-rig scans. The auto-levels black point also depends on the new exposure. |
| `foil_detect.py` weights, bias, threshold, `BRIGHT_V_THRESH=220` | **Invalid** | They encode the old directional-LED physics (e.g. "foils darker than ref"). |
| `frame_template_matcher` templates (built from scan logs) | Rebuild | Scan-domain templates. |
| `FRAME_THRESHOLD`, `_CONFIDENCE_MARGIN_SCALE`, `LIST_STAMP_THRESHOLD`, `SET_ICON_THRESHOLD/MARGIN` | Re-tune | None were tuned on scans in the first place (Phase 7 is pending). |
| Regression sets (`test_regression_362.py` images, foil label sessions 44/51/55/58/59) | **Re-capture** | Old-geometry images no longer represent production. Keep the old sets for algorithm-regression checks only. |
| `build_png_roi_templates.py` scan-log-driven set list | Change to build for **all** sets | Fixes B2 regardless of the redesign. |

### 5.3 Foil detection: candid assessment

**Current approach and why it is weak:**
- It infers foil-ness from **one** frame by comparing its brightness and saturation statistics with a *Scryfall render*. That render has a different tone curve, white point and exposure from the camera, so the signal is "how does this camera's rendering deviate from Scryfall's", which is confounded by art, border stock (cream old-border), exposure drift, auto white balance, printing mismatches (B7) and card age.
- The sign conventions are specific to one LED geometry ("foils darker than ref"). The in-sample 92 % P / 91 % R rests on partly self-labelled data with no holdout.
- It has no physical measurement of the thing that defines a foil: **view- and illumination-dependent reflectance**. The existing 2-view pair (+5 mm, a ~1° parallax change) had the right idea but a tiny angular change, and it is not used.
- The owner is right that this should be redesigned. The redesign is a good moment because the camera will be fixed and the card stationary, so multiple frames are pixel-registered for free.

**Options, ranked:**
1. **Multi-illumination capture (recommended primary).**
   - Setup: 2–4 LEDs at different azimuths/elevations around the upward camera, fired one at a time (or as 2 groups), with one frame per light state at fixed exposure.
   - Measurement: per pixel, compute a normalised intensity variation across light states and the hue shift. Matte ink is roughly Lambertian, so brightness changes smoothly and proportionally everywhere with no hue change. Foil (metallic + diffractive) produces large, spatially high-frequency, hue-shifting changes.
   - Score by the fraction of the card area whose inter-light ratio exceeds a bound, restricted to non-glare regions. **No reference image needed**, so it is independent of identification correctness. It also works for etched and old foils, since any metallic layer is strongly angle-dependent.
   - Cost: LED drivers on spare board outputs (or a small MCU), strobe/frame sync, and ~3 extra frames (~100–150 ms at 30 fps, less with triggering).
   - Bonus: the matte, glare-free combination (the per-pixel min or median across lights) is a better identification image.
2. **Polarisation difference.**
   - Setup: lens analyser plus two light sets, one cross-polarised (diffuse-only image) and one co-polarised or unpolarised (diffuse + specular). The difference is a specular map.
   - Metallic foil strongly preserves polarisation, while ink and paper depolarise. That gives a high-contrast foil mask, and the crossed image doubles as a glare-free ID image.
   - Cost: polariser film and analyser. It combines naturally with option 1 (make one light state co-polarised).
   - Caveat: glossy non-foil varnish also produces co-polarised specular. Keep lights off the mirror angle, and use the spatial texture of the specular map (foil sparkle/rainbow is textured; varnish glare is a smooth blob) to separate them.
3. **Multi-exposure while moving (zero hardware).**
   - Take 3–5 frames as the head carries the card across the camera. The angle change is larger than the current 5 mm pair, but frames need per-frame registration (warp each to canonical), and motion blur needs strobing or a short exposure.
   - A weaker and noisier signal than option 1. It is a reasonable prototype while the lighting hardware is built.
4. **Metadata prior (do now, free).**
   - Scryfall `finishes` is currently unused (no references in live code). If the disambiguated printing's `finishes == ['foil']` (or etched only), the card is foil; if it is `['nonfoil']`, the card is nonfoil. Only `['nonfoil','foil']` printings need a physical measurement.
   - Also gate by the `printing_disambiguated` confidence.
5. **Learned classifier:** only after options 1–2 produce a physically meaningful input stack. Use a small CNN or gradient-boosted features on the multi-light stack, with **human-verified** labels and a held-out session.

**Process fixes regardless of method:** build a ground-truth foil set labelled by a human (not detector output) across eras (old frame, etched, full-art lands, textured/galaxy/surge foils). Report held-out precision and recall at realistic prevalence. Log raw multi-light stacks during sorting so future re-tunes need no re-scan.

---

## 6. TODOs found

- `printing_disambiguation.py:12` — "Stage 3: Set icon (TODO Phase 6 — stubbed here)". Stale; Stage 3 is implemented.
- `plans/printing_disambiguation_plan.md` Phase 7 (labelled validation, `test_data/printing_disambiguation_labels.json`, threshold tuning for frame, stamp and icon) is **not done**. Phase 3 constants (0.55/0.55) and the Phase 6 constants in the plan (0.55/0.10) differ from what shipped (List 0.35, icon 0.40/0.05).
- `plans/printing_disambiguation_plan.md` Phase 4: `fetch_set_symbols.py` refresh not wired into `update_all.py`; confusable-pair short-circuit not wired (Phase 6 polish).
- `plans/foil_detection_plan.md`:
  - Priority 3: grayscale phash channel. Not implemented; the npz has only `a_packed`.
  - Priority 4: raise the threshold. Superseded by the 256-bit scale.
  - Phase 2: foil-aware identification path. Not implemented.
  - "Foil detection via lighting-angle change (impractical with fixed camera)" is listed as a non-goal. **That premise flips with the redesign.**
- `set_symbol_roi.py:21-28` — ROIs are "initial estimates … will need empirical tuning" (Phase 7).
- `frame_detect.py:39-40` — "Tune with diag data".
- `list_stamp.py` — threshold tuned on Scryfall images; "tune once real scans are available".
- `foil_detect.py:66-68` — "Re-tune via `_foil_tune.py` when lighting or camera changes significantly". This will be mandatory with the redesign.
- `card_detect.py:120-123` — "If the resulting crops look scrambled, flip the sign of dx_px" (foil pair sign is unverified).
- `card_art_id_handoff/AGENT_HANDOFF.md` §9 — two open decisions (the resize filter for the TS port, and the detector area fractions). `ts_scaffold/src/backend.ts` is a stub.
- `web_worker.py:2345-2352` — `_save_hash_diagnostics` is "DEPRECATED … now no-ops". The dead code after `return` should be removed.
