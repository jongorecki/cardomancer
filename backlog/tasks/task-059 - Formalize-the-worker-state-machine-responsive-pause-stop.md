---
id: TASK-059
title: Formalize the worker state machine; responsive pause/stop
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - refactor
  - worker
milestone: m-5
dependencies:
  - TASK-006
references:
  - docs/audit/02_server_worker_core.md
priority: medium
ordinal: 59000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Explicit states incl. busy sub-states; one locked transition table; pause/stop checked between steps.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Serial-error callback can't override estopped
- [ ] #2 Duplicate continuous chains prevented
- [ ] #3 Starting while paused ends prior session cleanly
- [ ] #4 Dead _cmd_cancel_calibration removed
<!-- AC:END -->
