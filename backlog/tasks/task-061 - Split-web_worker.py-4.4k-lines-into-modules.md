---
id: TASK-061
title: Split web_worker.py (4.4k lines) into modules
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - refactor
milestone: m-5
dependencies:
  - TASK-059
  - TASK-058
references:
  - docs/audit/02_server_worker_core.md
priority: medium
ordinal: 61000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Queue/state, card cycle, identification, routing, drop tuner, calibration, session persistence, image archiving. Best done alongside R8.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Shared overflow/pump-release logic
- [ ] #2 Tests pass
<!-- AC:END -->
