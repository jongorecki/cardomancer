---
id: TASK-004
title: Stop the Escape key from triggering emergency stop
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - frontend
  - safety
  - bug
milestone: m-0
dependencies: []
references:
  - docs/audit/05_frontend.md
priority: high
ordinal: 4000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
static/modules/estop.js:13 fires emergencyStop() on Escape anywhere, including when closing modals/tour/popovers mid-sort, forcing a re-home.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Escape no longer e-stops (remove, or require deliberate combo ignored while a modal is open)
- [ ] #2 On-screen E-STOP button unchanged
- [ ] #3 Shortcut (if kept) documented
<!-- AC:END -->
