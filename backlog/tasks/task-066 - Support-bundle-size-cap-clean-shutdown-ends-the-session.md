---
id: TASK-066
title: Support bundle size cap; clean shutdown ends the session
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - bug
milestone: m-5
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
priority: low
ordinal: 66000
---

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Bundle excludes scan image folders and is streamed/capped
- [ ] #2 Ctrl+C doesn't leave a stale session
<!-- AC:END -->
