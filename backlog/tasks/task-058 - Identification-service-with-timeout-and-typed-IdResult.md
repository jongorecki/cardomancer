---
id: TASK-058
title: Identification service with timeout and typed IdResult
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - robustness
  - identification
milestone: m-5
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
priority: medium
ordinal: 58000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
ID thread join has no timeout; _cmd_test_scan duplicates the path.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Timeout sends card to fallback
- [ ] #2 IdResult dataclass
- [ ] #3 test_scan uses the same service
<!-- AC:END -->
