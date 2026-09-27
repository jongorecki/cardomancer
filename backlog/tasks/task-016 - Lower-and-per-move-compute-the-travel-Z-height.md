---
id: TASK-016
title: Lower and per-move-compute the travel Z height
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
updated_date: '2026-09-27 18:55'
labels:
  - perf
  - motion
milestone: m-1
dependencies:
  - TASK-014
references:
  - docs/audit/01_motion_hardware.md
priority: low
ordinal: 16000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Z_CLEAR_HEIGHT=200 sits 80-150 mm above working surfaces; ~5.6 s/card of Z travel. Needs owner to measure bin-lip height.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Clear height derived from bin-lip height over the X span
- [ ] #2 Z travel per card down >50%
- [ ] #3 Card-hang collision test passes
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Owner (2026-09-27): Z=200 is roughly the real clearance for bins etc. plus a small margin; the audit's '80-150 mm too high' estimate is wrong. Not now: revisit only after the up-camera is integrated.
<!-- SECTION:NOTES:END -->
