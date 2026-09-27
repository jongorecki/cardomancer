---
id: TASK-009
title: Fix Windows path traversal in file routes
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - security
  - bug
milestone: m-0
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
priority: medium
ordinal: 9000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Backslashes pass filename checks: arbitrary read via /api/label/image, arbitrary delete via DELETE /api/bins/saved-configs.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 /api/label/*, /api/bins/saved-configs/*, /api/review/* reject `..` and backslashes and verify resolved path is inside the expected folder
- [ ] #2 Box-label HTML escaped
- [ ] #3 Tests with `..\` payloads
<!-- AC:END -->
