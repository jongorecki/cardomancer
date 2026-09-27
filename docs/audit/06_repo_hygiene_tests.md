# 06 — Repository hygiene, tests, and loose scripts

Audit date: 2026-09-27 · Branch: `docs/audit-and-backlog` (HEAD dcdef14) · Read-only audit. Nothing was moved or deleted.

## 1. Summary

- The root holds about 190 entries. About **75 of them are loose one-off Python scripts** (diag/diagnose/test_/prototype/eval/bench/...). Most were committed on 2026-04-18 (initial import) or 2026-04-22..24 (detection/set-symbol/foil investigations) and have not changed since.
- The real pytest suite (`tests/`) is healthy: **1329 passed, 1 skipped, 1 deselected (live), 0 failed, in 5m46s**. `pytest-timeout` and `pytest-cov` are not installed.
- 31 root `test_*.py` files are **not** pytest tests (pytest.ini sets `testpaths = tests`). They are hardware and scan-folder scripts, and their names are misleading.
- Repo: 532 tracked files. `.git` is 291 MB (pack 126 MB). Most of that comes from history: `backup/quasai.zip` (70 MB, untracked in acf8d77 but still in history) and three versions of `printings_map.json` (about 45 MB each, still tracked).
- A local `.env` exists. It is gitignored and **not tracked** (only `.env.example` is tracked). A check by filename found no tracked secrets.
- All 3 worktrees under `.claude/worktrees/` are fully merged into `main` and can be removed safely.
- There is no Python lint or format config (no ruff/black/pyproject/pre-commit) and no CI. The JS frontend has eslint and prettier via `package.json`.

## 2. Test results

Command: `python -m pytest -q -p no:cacheprovider` (`--timeout=120` was rejected because pytest-timeout is not installed).

```
1329 passed, 1 skipped, 1 deselected, 214 warnings in 345.86s
```

- All warnings are `datetime.utcnow()` DeprecationWarnings from `enrichment_db.py` lines 307/332/370/409.
- No hardware is needed. conftest mocks the hardware (see `test_hw_mock_hygiene.py`).
- Stale `pytest_output.txt` and `test_output.log` sit at the root (gitignored artifacts).

### Coverage gaps (counted by imports; no coverage tool is installed)

| Module | Lines | Test files importing it |
|---|---|---|
| web_server.py | 5514 | 18 |
| web_worker.py | 4359 | 7 (mostly pause/estop/session paths; the per-card sort loop has no unit tests) |
| web_calibration.py | 1862 | **0** |
| detection.py | 1588 | 1 |
| gcode_control.py | 1355 | 4 (all through mocks; nothing tests G-code emission or parsing) |
| web_camera.py | 883 | 1 |
| card_detect.py | 637 | **0** (only root diag/test scripts exercise it) |
| card_identify_hybrid.py | 174 | **0** |

Recommendations:
- Add pytest-cov and pytest-timeout to the requirements.
- Add golden-image tests for `card_detect.detect_card()` using a small committed fixture set. The logic to port already exists in the root `diag_test_regression.py` and `test_staging_detection.py`.
- Add G-code string tests for `gcode_control` against a fake serial port.

## 3. Loose script classification

Legend: T = tracked, U = untracked (gitignored). Actions:
- **KEEP**: runtime code; stays at the root or in the package.
- **TOOL**: move to `tools/<area>/`.
- **ARCHIVE**: move to `archive/`; only historical value.
- **DELETE**: superseded, and a copy exists in git history.

### Core-adjacent (referenced by runtime code or tests)

| Script | T | Last commit | Notes | Action |
|---|---|---|---|---|
| 16bit_rgb_create_card_hashes.py | T | 04-18 | `web_database.py:351` loads it with `import_module` | KEEP (rename later, with a shim) |
| create_card_hashes_v2.py | T | 05-21 | imported by `web_database.py:378` | KEEP |
| fetch_set_symbols.py | T | 04-22 | covered by `tests/test_fetch_set_symbols.py`; regenerates the set-symbol cache | TOOL (`tools/data/`) and update the test import |
| download_frame_fixtures.py | T | 04-24 | regenerates the frame_classifier test fixtures | TOOL (`tools/data/`) |
| find_frame_signatures.py | T | 04-18 | builds `frame_signatures.json`, which frame_detect.py uses | TOOL (`tools/data/`) |
| download_missing_frames.py | T | 04-18 | used together with audit_frame_coverage | TOOL (`tools/data/`) |
| audit_frame_coverage.py | T | 04-18 | checks frame fixture coverage | TOOL (`tools/data/`) |
| generate_foil_review.py | T | 04-27 | builds the per-session foil review HTML; still useful | TOOL (`tools/foil/`) |
| _foil_tune.py | T | 05-07 | refits foil weights (versioned on purpose, per .gitignore) | TOOL (`tools/foil/foil_tune.py`) |
| frame_label_tool.py | T | 04-24 | manual labeling UI for frame fixtures | TOOL (`tools/data/`) |
| benchmark_motion.py | T | 04-22 | motion speed benchmark, 767 lines | TOOL (`tools/bench/`) |
| bench_cascade_stages.py | T | 04-24 | cited in docs/speculative_bin_commit_PLAN.md | TOOL (`tools/bench/`) |
| bench_frame_matcher.py | T | 04-24 | | TOOL (`tools/bench/`) |

### Investigation and diagnostic one-offs

| Script(s) | T | Last commit | Notes | Action |
|---|---|---|---|---|
| diag_no_detect.py, diag_no_detect2.py, diag_no_detect3.py | T | 04-22 | no-detect frame investigation (resolved) | ARCHIVE |
| diag_test_3090/canny/cascade/final/full/mask.py | T | 04-22 | Canny/morph parameter sweeps that produced the 2-pass cascade | ARCHIVE |
| diag_test_regression.py | T | 04-22 | detector regression run over known-good frames | TOOL (`tools/diag/`), then port to pytest |
| diag_verify_warps.py | T | 04-22 | warp sanity check | ARCHIVE |
| diag_visualize.py, diag_visualize2.py | T | 04-22 | montage and visualisation | ARCHIVE |
| diag_printing.py, diag_set_symbol_fails.py | T | 04-24 | set-symbol and printing investigation | ARCHIVE |
| diagnose_bad_crops/border/edges/edges_zoom/nodet/refinement/remaining/scan_290.py (8 files) | T | 04-18 | earliest detection debugging, older than the current detector; they write into the diag_*/ folders | DELETE (history keeps them) |
| eval_frame_vs_manual.py, eval_set_symbol_akaze.py, eval_set_symbol_matcher.py | T | 04-23/24 | accuracy evaluators | TOOL (`tools/eval/`) |
| verify_roi_on_pngs.py, show_roi_overlay.py, roi_grid_overlay.py, sample_set_symbol_rois.py | T | 04-24 | set-symbol ROI tuning helpers | TOOL (`tools/set_symbol/`) |
| probe_foil_pairs.py | T | 04-24 | foil pair probe | ARCHIVE |
| prototype_foil_signal1.py, prototype_foil_multisignal.py, foil_review_page.py | T | 04-18/22 | prototypes, replaced by foil_detect.py and generate_foil_review.py | ARCHIVE |
| _foil_candidates_review_r3.py | T | 04-23 | round-3 review page | ARCHIVE |
| _foil_candidates_review.py, _r2.py, _foil_signal_audit.py, _db_inspect.py, _noref_check.py, _otag_smoke.py | U | — | local only, gitignored | DELETE locally (or move to tmp/) |
| hashing_backup_20260412.py | T | 04-18 | manual backup copy of hashing.py | DELETE (git history has it) |

### Root `test_*.py` files (NOT pytest; hardware and scan-folder scripts)

| Script(s) | Notes | Action |
|---|---|---|
| test_query_parser.py (830 lines), test_sort_config.py (519 lines) | overlap with `tests/test_query_parser.py` and `tests/test_sort_config.py` | check that `tests/` covers the same cases, then DELETE |
| test_staging_detection.py, test_detection_live.py, test_sorting_live.py, test_full_sort.py, test_10_cards.py | live hardware runners; a comment in web_worker refers to the test_staging_detection flow | TOOL (`tools/hw/`, renamed to `run_*.py`) or ARCHIVE |
| test_full_pipeline.py, test_detect_only.py, test_new_detection.py, test_border_detection.py, test_layout_detection.py, test_regression_362.py | offline runs over scan_logs | TOOL (`tools/eval/`, renamed to `eval_*.py`) |
| test_embeddings.py, test_hybrid_compare.py, test_hybrid_decision.py, test_phash_regions.py, test_methods_compare.py, test_grayscale_phash_foil.py, test_hash_selftest.py | identification method experiments | ARCHIVE (except test_hash_selftest, which becomes a TOOL) |
| test_ocr.py, test_ocr_accuracy.py, test_ocr_scans.py | OCR experiments | ARCHIVE |

### Legacy webcam prototypes

| Script(s) | Notes | Action |
|---|---|---|
| webcam_opencv_test.py, rgb_hash_webcam_opencv_test.py, o1mini_rgb_hash_webcam_opencv_test.py, smaller_art_area_webcam_detection.py, smaller_art_hashing_rgb.py | earliest prototypes; copies with the same names already exist in `backup/` | DELETE (history keeps them) |

## 4. backup/ and archive/

- **`backup/`** (68 MB on disk; 54 files tracked across backup/ and archive/):
  - Duplicate snapshots of old scripts, plus `detection_DEPRECATED.py` and `hashing_DEPRECATED.py`.
  - `card_hashes*.json` (29 MB each). These are untracked because of the `card_hashes*.json` glob.
  - `frame_signatures.json` (8.3 MB). This one is **tracked** and duplicates the root copy.
  - Frontend snapshots in `20260418_phase0a/` and `20260419_204505/`.
  - The README describes backup/ as "not for tracking", but files in it are tracked.
- **`archive/`** (25 MB): old scripts plus tracked debug images in `archive/debug/*.png`.
- Recommendation: stop tracking `backup/` entirely (`git rm -r --cached backup`, then add `backup/` to .gitignore). Keep `archive/` for code only and stop tracking `archive/debug/`.

## 5. Git tracking and size

`git count-objects -vH` reports 2186 objects, a 126.55 MiB pack, 291 MB for `.git` on disk, and 3 leftover tmp_obj files (harmless; `git gc` removes them).

Tracked files larger than 5 MB:

| File | Size |
|---|---|
| printings_map.json | 45 MB (3 versions in history, about 135 MB raw) |
| frame_signatures.json | 8.4 MB |
| backup/frame_signatures.json | 8.4 MB (duplicate) |

Largest blobs in history:
- `backup/quasai.zip`, 70 MB. Removed from the tree in acf8d77 but still in history.
- `printings_map.json`, 3 versions.
- `staging_bg_ref.png`, 6 versions of about 2 MB each. This calibration image is specific to one machine and changes on every calibration, so it should not be tracked.

Already ignored correctly: `.env`, `*.db`, `default-cards-*.json` (9 local copies; only the newest is needed), `AtomicCards.json`, `card_hashes*`, `card_embeddings.npz`, logs, `diag_*/`, `debug_*`, and the foil review HTML files.

Tracked, but probably should not be:
- `printings_map.json` (generated from the Scryfall bulk data)
- `staging_bg_ref.png`
- machine-specific state: `staging_calibration.json`, `staging_roi.json`, `_last_setup.json`, `setup_*.json`
- `backup/**`
- `archive/debug/*`

Local clutter (ignored, but on disk):
- `diag_290/ diag_bad_crops/ diag_border/ diag_edges/ diag_edges_zoom/ diag_nodet/ diag_out/ diag_output/ diag_refinement/ diag_remaining/ diag_scan205/`
- `debug_crops/ debug_warps/`, `layout_test_failures/`, `foil_signal1/`, `tmp/`
- 9 `default-cards-*.json` files
- `tune_run*.txt`, `sim_*.log`, `download_missing_frames.log`, `pytest_output.txt`, `test_output.log`

## 6. Worktrees (`git worktree list`)

| Path | Branch / HEAD | Commits not in main | Uncommitted changes |
|---|---|---|---|
| .claude/worktrees/agent-a07a399362584c35a | worktree-agent-a07a399362584c35a @ 3ea7f31 (2026-05-20 merge of feature/quick-wins) | none | only `.claude/settings.local.json` |
| .claude/worktrees/agent-af7342877279c0518 | worktree-agent-af7342877279c0518 @ 08241ad (2026-05-20 merge of feature/code-side-finishup) | none | only `.claude/settings.local.json` |
| .claude/worktrees/interesting-galileo-1fc6c1 | detached @ e0cb013 (2026-05-21 logging fix) | none (e0cb013 is an ancestor of main) | clean |

All three can be removed safely with `git worktree remove --force`. After that, delete the branches `worktree-agent-a07a399362584c35a`, `worktree-agent-af7342877279c0518` and `claude/interesting-galileo-1fc6c1` with `git branch -d`.

The other local branches were outside this audit's scope: feature/design-system-skin, phase4-staged-sort-tab, polish-batch-2, predicates-and-polish, single-instance-guard, welcome-tour-polish. Run `git branch --no-merged main` before pruning any of them.

## 7. Outside the repo

- `D:\Card_Sorter` is **not** a git repo. It holds `Marlin-2.1.3-b1/` plus `Configuration.h` and `Configuration_adv.h`, so the gantry's firmware config is not under version control. Recommendation: add a `firmware/` folder to this repo (or create a separate repo) with the two Configuration files and a note of the Marlin version and board.
- `D:\Card_Sorter\card_art_id_handoff` is a separate repo (`github.com/jongorecki/cardomancer-card-detection`). The README should link to it.

## 8. Tooling, dependencies, dev setup

- **requirements.txt**:
  - Core libraries (Flask, flask_socketio, opencv-python, numpy, pillow, imagehash, pyserial, flask-cors) are **unpinned**.
  - torch has only a minimum version, which is deliberate.
  - Enrichment libraries are pinned to minor versions.
  - There is no lockfile and no Python version file.
- **Test dependencies**: only pytest and pytest-mock. pytest-timeout and pytest-cov are missing.
- **Lint and CI**: no Python linter or formatter config, no pre-commit, no CI (`.github/` does not exist). The JS side has an eslint 9 flat config and prettier.
- **README Quickstart**, gaps found:
  - It gives Python 3.11+ and `pip install -r requirements.txt`, but the step to install torch from pytorch.org first appears only in comments inside requirements.txt.
  - The "build a hash DB" step is vague ("see web_database.py").
  - There is no venv step.
  - It does not mention the size of the `default-cards` download, how to regenerate `printings_map.json`, or which `.env` keys are required.
  - The repo-layout section describes `backup/` as untracked, which is false.

## 9. Proposed target layout

```
Scripts/
  README.md  pyproject.toml (ruff, pytest config)  requirements*.txt  package.json
  cardomancer/            (later: core modules as a package; phase 2, optional)
  *.py                    core runtime modules (unchanged in stages 1-3)
  static/ templates/ sort_configs/ bin_configs/ aruco_markers/ card_data/
  tests/                  pytest suite (+ tests/fixtures/detection/ golden images)
  tools/
    data/       fetch_set_symbols, download_frame_fixtures, find_frame_signatures,
                download_missing_frames, audit_frame_coverage, frame_label_tool
    foil/       foil_tune, generate_foil_review
    bench/      benchmark_motion, bench_cascade_stages, bench_frame_matcher
    eval/       eval_*, former test_full_pipeline/test_detect_only/...
    set_symbol/ verify_roi_on_pngs, show_roi_overlay, roi_grid_overlay, sample_set_symbol_rois
    hw/         former test_staging_detection/test_sorting_live/...
    generate_openapi.py, memory_profile_session.py (already there)
  archive/                historical scripts only, with a README index
  docs/ plans/ backlog/
  firmware/               Marlin Configuration*.h (+ version note)
  data/ (gitignored)      default-cards, AtomicCards, hashes, embeddings, dbs, printings_map
  var/  (gitignored)      logs, scan_logs, debug_*, diag_*, tmp
```

## 10. Staged cleanup plan

Each stage is one PR, and the test suite must stay green after each one.

1. **Zero-risk removals**:
   - Remove the 3 worktrees and their merged branches.
   - Delete the ignored local clutter: diag_*/ folders, all old default-cards files except the newest, logs, tune_run files, pytest_output.
   - Run `git gc`.
2. **Stop tracking** (`git rm --cached`):
   - backup/**, archive/debug/**, staging_bg_ref.png.
   - The machine-specific JSON files, but only after confirming the app regenerates them or runs without them. Add `*.example.json` files where needed.
   - Extend .gitignore to match.
3. **Delete duplicates**:
   - The legacy webcam prototypes, hashing_backup_20260412.py and the diagnose_*.py files.
   - The root test_query_parser.py and test_sort_config.py, after confirming `tests/` covers the same cases.
4. **Move to tools/ and archive/** with `git mv`, which keeps history:
   - Fix imports: either insert the repo root into `sys.path` in each tool, or run tools as `python -m tools.x`.
   - Update tests/test_fetch_set_symbols.py and any references in docs and the README.
   - Add an index in `archive/README.md`.
5. **Tooling**:
   - Add a pyproject.toml with ruff for lint and format (start permissive), and move the pytest config into it.
   - Add pytest-timeout and pytest-cov.
   - Pin core dependencies: `pip freeze` into requirements-lock.txt.
   - Add GitHub Actions that run pytest and eslint on push.
6. **Tests**:
   - Golden-image tests for card_detect and card_identify_hybrid.
   - G-code emission tests.
   - web_calibration route tests.
   - Fix the utcnow deprecations.
7. **Optional history rewrite**: use git filter-repo to drop quasai.zip and the old printings_map blobs. Do this only with explicit owner approval. It requires a force-push and fresh clones of every worktree.
8. **Firmware**: import the Marlin Configuration*.h files into `firmware/`.
