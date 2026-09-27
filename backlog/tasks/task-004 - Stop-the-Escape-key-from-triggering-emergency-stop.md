---
id: TASK-004
title: Stop the Escape key from triggering emergency stop
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-27 18:19'
updated_date: '2026-09-27 19:06'
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

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
Remove the Escape keydown binding in static/modules/estop.js; keep the on-screen button.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Binding removed; no docs referenced it. Not yet verified in a browser: open the UI, close a modal with Escape mid-sort and confirm no e-stop, then confirm the E-STOP button still stops.
<!-- SECTION:NOTES:END -->
