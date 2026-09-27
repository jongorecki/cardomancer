---
id: TASK-065
title: Make undo consistent across all records
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - bug
milestone: m-5
dependencies:
  - TASK-021
references:
  - docs/audit/02_server_worker_core.md
priority: low
ordinal: 65000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Undo should remove scan_history row, adjust inventory by collector number + foil, mark CSV row undone.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Consistent undo
- [ ] #2 Unavailable after session end
<!-- AC:END -->
