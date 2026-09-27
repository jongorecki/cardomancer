---
id: TASK-005
title: Bound probe moves and clamp X/Z in software
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
updated_date: '2026-09-27 18:47'
labels:
  - safety
  - firmware
milestone: m-0
dependencies:
  - TASK-002
references:
  - docs/audit/01_motion_hardware.md
priority: high
ordinal: 5000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
`M211 S0` disables all soft endstops and probes use `G38.2 Z-999`, so a failed probe has no lower bound.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 G38.2 uses bounded absolute targets instead of Z-999; a failed probe stops at the bound with an error
- [ ] #2 Host-side X clamp to the real rail length alongside the existing clamp_z
- [ ] #3 Bins at X>800 still reachable
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Re-enabling firmware soft endstops (raise X_MAX_POS, remove M211 S0) needs a reflash: TASK-107.
<!-- SECTION:NOTES:END -->
