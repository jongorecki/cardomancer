# 07 — Docs / TODO Mining

Audit date: 2026-09-27. Branch `docs/audit-and-backlog`. Last code commit: `dcdef14` (2026-06-04).
Scope: README.md, PROJECT.md, docs/, plans/ (incl. handoff/, design-system/), `D:\Card_Sorter\card_art_id_handoff\`.
Status was set by grepping the code, not by trusting the docs.

**Redesign context:** the staging platform goes away. Cards are loaded face-down, and an **upward-facing camera images each card while the suction head holds it**. Items marked **OBSOLETE-BY-REDESIGN** only make sense with a staging platform. Items marked **AFFECTED** need re-scoping.

Legend: DONE / PARTIAL / NOT STARTED / OBSOLETE / REFERENCE (a rubric or spec with no deliverable).

---

## 1. Document inventory

| Path | Topic | Date (git / header) | Status | Stale? |
|---|---|---|---|---|
| README.md | Host software overview | 2026-05-07 | REFERENCE | **Stale.** Says "five tabs" (the UI is now Home/Sort/Collection/Setup + gear), "static/app.js all front-end JS in one file" (now split into `static/modules/*`), and describes the staging-platform flow. Its Roadmap lists frame detection, foil detection, enrichment wiring and overflow as future work, but all of those have shipped. |
| PROJECT.md | Whole system (hardware, firmware, host) | 2026-05-07 | REFERENCE | **Stale.** Staging platform is central, "Build state" is frozen at ~Phase 0B, lighting is "not yet installed", ArUco ID 49 is the staging reference, and the "app.js one file" principle is now outdated. |
| docs/README.md | OpenAPI docs how-to | 2026-05-21 | DONE | Mostly current. One contradiction: it says "`openapi-overlay.yaml` (not yet created)" and then describes the shipped `openapi-overlay.json`. |
| docs/openapi.json, openapi-overlay.json | Generated spec + overlay | 2026-05-21 | DONE | Generated from code. |
| docs/cycle_time_findings_2026-07-19.md | Cycle time (15–18 s/card) analysis | 2026-07-19, **untracked** | PARTIAL / needs verification | **Partly contradicted by the code.** `gcode_control._probe_with_cache` already fast-moves to `cached_z + Z_APPROACH_MARGIN (10 mm)` before G38.2, which is a two-stage descent. The host sends `G38.2 ... F3600` (60 mm/s), and an explicit F on G38 normally takes precedence over `Z_PROBE_FEEDRATE_FAST`. So "5 mm/s is the whole problem" (Finding 1) must be measured on the machine. The file has 49 `M400`s, which is consistent with Finding 4. |
| docs/speculative_bin_commit_PLAN.md | Pre-move X toward the likely bin during ID | 2026-07-19, **untracked** | NOT STARTED (gated on < 4 s cycle) | **AFFECTED**: on-head imaging means the card is already on the cup during ID, which makes the idea more attractive. |
| plans/autonomy_ladder.md | Autonomy-level rubric | 2026-05-07 | REFERENCE / mostly DONE | Roadblocks #1–#7 shipped 2026-05-07. Open: L3 hash-DB install prompt, "apply correction to similar", accumulating wishlist toast, camera-offset suggestion. |
| plans/bulk_edit_plan.md | Bulk edit + clipboard + provenance | 2026-04-27 | **NOT STARTED** | No `provenance`, `inventory_history` or bulk-edit code. |
| plans/foil_detection_plan.md | Foil detection + DB-coverage root cause | 2026-04-18 | PARTIAL | Frame-dedup fix DONE (`download_cards.py` groups by `(illustration_id, frame)`). Foil detector DONE (Level-5). Grayscale phash channel NOT shipped (prototype `test_grayscale_phash_foil.py` only). Scans 217/299 misID still open. |
| plans/foil_retune_2026-05-07.md | Level-5 foil retune report | 2026-05-07 | DONE (record) | Open: spot-check 4 FPs; old-physics foils are still FN. **AFFECTED**: tuned to staging optics. |
| plans/legal_prep.md | Legal / ToS landscape | 2026-05-07 | NOT STARTED (lawyer) | Current as research. |
| plans/ooux_inventory.md | OOUX object model | 2026-05-07 | REFERENCE | Validation study deferred. |
| plans/printing_disambiguation_plan.md | Frame / List / set-icon cascade | 2026-04-22 | PARTIAL: Ph 1–6 DONE, **Ph 7 NOT STARTED** | No labels file. Confusable short-circuit not wired. `fetch_set_symbols` not in `update_all.py`. |
| plans/query_helper_plan.md | Otag knowledge layer + Query Helper + viz | 2026-04-28 | Ph 4 (translator) DONE; Ph 1/3 built then **REMOVED** (commits `7b0a9e8`, `d1c50dd`; note in `enrichment_db.py`); Ph 2 NOT STARTED | The doc does not record the removal. |
| plans/right_click_menu_plan.md | Universal card context menu | 2026-04-27 | **NOT STARTED** | No `contextmenu` handler anywhere. |
| plans/sort_flow_stages.md | Staged Sort tab | 2026-05-07 | Mostly DONE (Phase 4 pts 1–5; history + review queue 2026-05-20) | Cites the e-stop bug as open (fixed 2026-05-07). "Camera view of staging platform" is **AFFECTED**. |
| plans/web_enrichment_plan.md | Master enrichment plan | 2026-04-18 | Mostly DONE | Open: divider auto-advance, gallery/analytics, Discord/ntfy, deck-usage overlay, strictlybetter, coverage health. |
| plans/web_enrichment_source_probes.md | Probe reference | 2026-04-18 | REFERENCE / DONE | Tagger GraphQL turned out to be auth-walled; the search-API fallback is in use. |
| plans/z_bounce_retry_plan.md | Z-bounce on no-detect retry | 2026-04-22 | **DONE** | `_z_bounce_at_contact`, `tests/test_z_bounce.py`. Distance tuned to 10 mm. Line refs are stale. Live validation not recorded. **AFFECTED** (trigger). |
| plans/handoff/README.md + 01–11 | Enrichment implementation handoff | 2026-04-18/19 | Mostly DONE / historical | Scope fences (04) still fence `web_camera.py`, `card_detect.py`, `staging_*`, `calibrate_staging.py` and motion, so **the redesign must lift them**. Phase 4 acceptance criteria partly open. |
| plans/handoff/design_chat.txt | Design chat transcript (180 KB) | 2026-04 | Historical | Skimmed only. |
| plans/design-system/* | Cardomancer brand system | 2026-05-07 | DONE (re-skin `4e72a96`) | README copy describes staging. Marketing site not built. |
| ../card_art_id_handoff/{README,SPEC,AGENT_HANDOFF}.md, ts_scaffold/README.md | On-device mobile art-ID export (separate repo) | 2026-06-17/18 | PARTIAL | Python reference DONE. TS `backend.ts` stub. `detect.ts` not started. No production DB. 2 open decisions. Contains a **staging-free generic detector** that is relevant to the redesign. |

Other untracked items seen: `CLAUDE.md`, `backlog/`. They are outside this audit.

---

## 2. Extracted TODOs / open items / known issues / ideas

Format: item, then source §section, then status and note.

### 2.1 Hardware / motion / cycle time
1. **X-axis step skipping** (rod alignment, 686ZZ/625ZZ idler, pillow blocks, belt offset; 1.5–1.7 A motor as fallback). Sources: README §Known issues; PROJECT §Known mechanical issues. OPEN (hardware).
2. **X-carriage squeak**: add a third pillow block. Source: PROJECT §Known mechanical issues. OPEN.
3. **Lighting install** (high-CRI LED strip). Source: PROJECT §Lighting. OPEN, **AFFECTED** (the upward camera needs its own lighting).
4. **Two-stage probe descent**. Source: cycle_time §1. PARTIAL/DONE: the approach cache exists. Remaining: measure the real time breakdown, tighten the 10 mm margin, add a fallback for missed contact.
5. **Audit unconditional `M400` in gcode_control for move blending**. Sources: cycle_time §4; speculative §3.3/§6 Q2. OPEN. Keep the Z/X no-overlap rule.
6. **S-curve + X accel > 500** (firmware), gated on the new motor. Source: cycle_time §2–3. OPEN.
7. **Junction deviation 0.08 → 0.2–0.5** after a slip test. Source: cycle_time §4. OPEN.
8. **Bottom-side vision** removes a descent and the staging re-pick. Source: cycle_time §Priority 3. **This is the redesign.**
9. **Speculative bin commit** phases 1–5. Source: speculative_bin_commit_PLAN. NOT STARTED (gated). Phase 1 per-card timing instrumentation is useful now.
10. **Cheap early-exit bin classifier** (~20 ms). Source: speculative §6 Q4. IDEA.
11. **Z-bounce live validation**. Source: z_bounce §Step 4. UNVERIFIED.
12. **Ramp-style overflow bin**. Sources: README Roadmap; PROJECT §Planned. OPEN (hardware).
13. **Second-camera auto-bin-calibration**. Sources: README Roadmap; PROJECT §Planned. OPEN, **AFFECTED**.
14. **Sleeved-card handling**. Source: README Roadmap. NOT STARTED.
15. **Condition grading**. Source: README Roadmap. NOT STARTED.

### 2.2 Identification / vision
16. **Grayscale phash 4th channel + threshold 30→35**. Source: foil plan §Revised priority 3–4, §Phase 3. NOT STARTED.
17. **Foil-aware ID path / DINO cross-check**. Source: foil plan §Phase 2. NOT STARTED (likely unnecessary).
18. **Upside-down misID on scans 217/299** (fine alignment). Source: foil plan §Separate issue. OPEN, **AFFECTED**.
19. **Etched / pre-2010 foil false negatives**. Sources: foil plan Open Q2; retune §Qualitative. OPEN.
20. **Spot-check 4 flagged foil FPs**. Source: retune §Regression Flagged. OPEN.
21. **Re-tune the foil detector for the new optics**. Implied by the redesign. REQUIRED.
22. **Printing disambiguation Phase 7** (labeled set, parametrized test, threshold tuning). Source: printing plan §Phase 7. NOT STARTED. Do it after the camera change.
23. **Confusable set-icon short-circuit**. Source: printing plan §Phase 6 polish. NOT STARTED.
24. **Hook `fetch_set_symbols.py` into `update_all.py`**. Source: printing plan §Phase 4 polish. NOT STARTED.
25. **Set-icon ROI calibration / 32 px rung / alias curation**. Source: printing plan §Phase 4–5. OPEN, **AFFECTED**.
26. **Disambiguation badge in the UI**. Source: printing plan §Phase 3. UNVERIFIED.
27. **Same-set variants, pre-8ED, borderless fallback**. Source: printing plan §Non-goals/Open Q. FUTURE.
28. **Face-down loading / card-back handling re-validation**. Implied by the redesign.
29. **Staging-free detector for on-head imaging** (back-port from `card_art_id_handoff/reference/detect.py`). Source: AGENT_HANDOFF §3. OPEN, redesign-critical.
30. **Threshold doc mismatch** (sorter "≤82" vs. art-ID "120/100"). DOC.

### 2.3 Sort flow / autonomy
31. **L3 "new card data — install?" with atomic DB swap + rollback**. Source: autonomy §Data. PARTIAL: an "Update available" hint exists.
32. **"Apply correction to similar pending matches"**. Sources: autonomy §Review; sort_flow §Post-sort. NOT STARTED.
33. **Accumulating wishlist toast**. Sources: autonomy §Wishlist; sort_flow Resolved #6. PARTIAL (log line only).
34. **Camera-offset suggestion after ArUco**. Source: autonomy §Calibration. UNVERIFIED, **AFFECTED**.
35. **Drop-tuner suggested defaults**. Source: autonomy §Calibration. UNVERIFIED.
36. **Empty-bin → box/divider assignment prompt**. Source: sort_flow §Running. UNVERIFIED.
37. **Per-session CSV export action**. Source: sort_flow §Post-sort. PARTIAL (`scans.csv` is written on disk).
38. **Multi-pass / re-sort value question**. Sources: sort_flow; ooux. OPEN.
39. **"Session in progress" navbar indicator**. Source: sort_flow §Navigating away. UNVERIFIED.
40. **No-detect hint text mentions staging**. Source: sort_flow. **AFFECTED**.

### 2.4 Collection / enrichment / integrations
41. **Bulk Edit Phase A**. Source: bulk_edit_plan. NOT STARTED.
42. **Bulk Edit Phases B–E**. NOT STARTED. Phase F deferred.
43. **Right-click menu Phase A + sort-session kebab flag editor**. Source: right_click_menu_plan. NOT STARTED. Depends on #41.
44. **Query Helper UI (Phase 2)**. Source: query_helper_plan. NOT STARTED. Hierarchy relaxation is blocked because the otag layer was removed.
45. **Divider auto-advance + `storage_divider_advanced` + printable range label**. Sources: web_enrichment §12; handoff 03. NOT STARTED.
46. **Session gallery + analytics**. Sources: web_enrichment §20; handoff 03. NOT STARTED.
47. **Wishlist notifications (browser / Discord / ntfy)**. Sources: web_enrichment §21; README env. NOT STARTED (env vars documented, no code).
48. **Moxfield deck-usage overlay**. Sources: web_enrichment §18; handoff 03. Likely NOT STARTED.
49. **Moxfield `by_category` / `prefer_listed`**. Source: handoff 03. PARTIAL.
50. **Coverage health / stale-source banners**. Source: web_enrichment §Verification. UNVERIFIED.
51. **Cross-source sanity test**. Source: web_enrichment §Verification 4. UNVERIFIED.
52. **strictlybetter.eu / MTGStocks / Archidekt**. DEFERRED.
53. **Calibration wizard drift regression check**. Source: handoff 03. PARTIAL, **AFFECTED**.
54. **Performance targets**. Source: handoff 03. UNVERIFIED.

### 2.5 Legal / product
55. **Lawyer review (10 questions)**. Source: legal_prep §Decisions. NOT STARTED.
56. **"Unofficial / not affiliated" disclaimer in the UI**. Source: legal_prep §Mitigations. UNVERIFIED.
57. **Replace CK/EDHREC scraping with sanctioned sources**. Source: legal_prep. OPEN decision.
58. **OOUX card-sort user study**. Source: ooux. DEFERRED.
59. **Marketing site**. Source: design-system README. NOT STARTED.

### 2.6 Documentation debt
60. **Rewrite README / PROJECT** for the current IA, modules, shipped features and the redesign.
61. **Stamp accurate status headers on the plans** (query_helper removal, z_bounce done, sort_flow done, foil dedup done).
62. **Revise scope fences (handoff/04) for the redesign.**
63. **docs/README overlay yaml/json contradiction.**
64. **Track or relocate the untracked docs and correct cycle_time Finding 1.**

### 2.7 card_art_id_handoff
65. **Implement `ts_scaffold/src/backend.ts`**. NOT STARTED.
66. **Decide the resize filter**. OPEN DECISION.
67. **Decide the area fractions**. OPEN DECISION.
68. **Port `detect.ts`, build the production `cardhashes.bin`, camera screen**. NOT STARTED.

---

## 3. Redesign impact (staging platform removed)

- **Obsolete:** the staging drop/re-pick. ArUco ID 49 staging marker. `calibrate_staging.py`, `generate_staging_mat.py`, `staging_calibration.json`. The staging-ROI troubleshooting entry. The +5 mm X paired foil frame (commit `8b912b9`). Staging camera-position moves.
- **Affected:** #3, #9, #11, #13, #18, #21, #22, #25, #28, #29, #34, #40, #53, #60, #62.
