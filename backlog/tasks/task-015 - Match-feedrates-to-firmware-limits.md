---
id: TASK-015
title: Match feedrates to firmware limits
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
updated_date: '2026-09-27 18:47'
labels:
  - motion
  - perf
milestone: m-1
dependencies:
  - TASK-002
references:
  - docs/audit/01_motion_hardware.md
priority: high
ordinal: 15000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
X_FEEDRATE 27000 (450 mm/s) exceeds 350 mm/s firmware max and is silently capped.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 No move requests exceed M203 limits
- [ ] #2 Startup reads M503 and logs/warns on effective limits
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Host-side only: clamp requested feedrates to M203 values read at startup. No firmware changes.
<!-- SECTION:NOTES:END -->
