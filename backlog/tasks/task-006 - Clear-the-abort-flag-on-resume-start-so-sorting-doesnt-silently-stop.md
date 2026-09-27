---
id: TASK-006
title: Clear the abort flag on resume/start so sorting doesn't silently stop
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - bug
  - worker
milestone: m-0
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
`_abort_requested` survives resume, session start and cancel; after a camera auto-pause or stray calibration Cancel every card cycle exits immediately.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 resume/start_session/start_continuous/detect clear the flag
- [ ] #2 Test: camera auto-pause then Resume keeps sorting
- [ ] #3 /api/calibration/cancel only sets the flag while calibration is running
<!-- AC:END -->
