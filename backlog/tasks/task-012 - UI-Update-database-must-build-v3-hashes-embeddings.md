---
id: TASK-012
title: UI 'Update database' must build v3 hashes + embeddings
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - bug
  - data
  - identification
milestone: m-0
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 12000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The UI rebuild and session preflight use v1/v2 hash DBs while identification uses card_hashes_v3 + DINOv2 embeddings.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 UI button runs the update_all phases
- [ ] #2 Preflight/startup check verify v3 + card_embeddings.npz
- [ ] #3 v1/v2 generation and config.py self-rewrite removed
<!-- AC:END -->
