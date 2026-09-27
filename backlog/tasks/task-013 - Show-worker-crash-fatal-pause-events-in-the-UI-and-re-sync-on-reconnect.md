---
id: TASK-013
title: Show worker crash/fatal/pause events in the UI and re-sync on reconnect
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - frontend
  - reliability
  - bug
milestone: m-0
dependencies: []
references:
  - docs/audit/05_frontend.md
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 13000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Backend emits worker_crashed, worker_fatal, session_paused (incl. 'serial disconnected'), estop_reset_complete; frontend has no handlers and doesn't re-sync after reconnect.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Persistent, screen-reader-announced banner for crash/fatal; pause shows its reason
- [ ] #2 After server restart the state badge and sort stage are correct without reload
- [ ] #3 Blocking alert() on e-stop replaced by a banner
<!-- AC:END -->
