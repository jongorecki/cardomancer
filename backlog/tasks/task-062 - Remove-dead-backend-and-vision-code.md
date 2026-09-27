---
id: TASK-062
title: Remove dead backend and vision code
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - cleanup
milestone: m-5
dependencies:
  - TASK-012
references:
  - docs/audit/02_server_worker_core.md
  - docs/audit/03_vision_identification.md
  - docs/audit/01_motion_hardware.md
priority: low
ordinal: 62000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
main.py, hashing.py, ocr.py, layout_signatures.py, old hash builders, _save_hash_diagnostics, legacy custom_queries and /api/sort/modes, multi-source code, get_nearest_source_x, etc.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Live path imports no archived module
- [ ] #2 Tests pass
<!-- AC:END -->
