# Codebase audit — 2026-09-27

A file-by-file audit of the sorter, done ahead of the up-camera redesign. Every finding that needs work is tracked as a task in `backlog/` (run `backlog board` or `backlog browser`). Task references point back to these docs.

| Doc | Covers |
|---|---|
| [01_motion_hardware.md](01_motion_hardware.md) | G-code layer, firmware configs, calibration, per-card cycle timing, up-camera impact analysis and open questions |
| [02_server_worker_core.md](02_server_worker_core.md) | web_server / web_worker architecture, state machine, camera capture, persistence, security |
| [03_vision_identification.md](03_vision_identification.md) | Live detect → identify → printing → foil pipeline, dead code, foil options for the redesign, card_art_id_handoff |
| [04_data_sorting_enrichment.md](04_data_sorting_enrichment.md) | DB schemas, query language, bin routing, enrichment scrapers, legal risk |
| [05_frontend.md](05_frontend.md) | static/ and templates/, UX flows, staging references to remove |
| [06_repo_hygiene_tests.md](06_repo_hygiene_tests.md) | Loose-script classification, tracked large files, worktrees, test results, tooling |
| [07_docs_todo_mining.md](07_docs_todo_mining.md) | Status of every plan/doc vs. the code, and the open to-dos extracted from them |

## Headline findings

- **Safety:** the auto safety reset lowers Z to 0 (fully down) instead of lifting it (`web_worker.py:489`, TASK-001). The E-stop isn't reliable: there's no serial write lock, EMERGENCY_PARSER is off, and M112 can't be recovered with M999. The Escape key triggers the e-stop. Soft endstops are disabled.
- **Firmware:** two different Marlin config pairs exist and neither is in git. Only an M503 dump can say which values are live.
- **Where the ~13–14 s cycle goes:** about 5.6 s of Z travel (owner confirms Z=200 is the needed clearance, so this stays for now), about 3.3 s of X travel, about 3.3 s of fixed sleeps and dwells, and about 1 s of overhead. The foil B-frame costs about 1 s and nothing uses it.
- **Tests:** 1329 pass, 1 skipped, 0 fail (about 6 min, no hardware needed). Detection, identification, calibration and the card cycle have little or no coverage.

## Milestones

M1 safety → M2 quick cycle-time wins on today's hardware → M3 up-camera redesign (starts with TASK-024 "owner design decisions") → M4 hygiene/tests/tooling → M5 identification & foil → M6 optimization/refactor → M7 frontend (long-term) → M8 features/data.
