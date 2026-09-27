---
id: TASK-008
title: Put a held card down safely on abort or error
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - safety
  - worker
milestone: m-0
dependencies:
  - TASK-001
references:
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 8000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Abort currently drops the card mid-air or leaves it on staging (web_worker.py:1491-1505).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Abort while holding places the card in fallback bin (or back at source) before pumps off
- [ ] #2 Test covers each abort point
<!-- AC:END -->
