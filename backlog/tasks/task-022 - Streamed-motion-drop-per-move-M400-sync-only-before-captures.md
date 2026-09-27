---
id: TASK-022
title: 'Streamed motion: drop per-move M400, sync only before captures'
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
updated_date: '2026-09-27 18:47'
labels:
  - perf
  - firmware
  - motion
  - firmware-deferred
milestone: m-1
dependencies:
  - TASK-002
  - TASK-003
  - TASK-007
  - TASK-014
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/07_docs_todo_mining.md
priority: low
ordinal: 22000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Enable LASER_SYNCHRONOUS_M106_M107 + ADVANCED_OK; then evaluate S-curve, input shaping, junction deviation 0.2-0.3 with a card-slip test.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Pick and drop sequences each have one sync point
- [ ] #2 Pump switching synchronized with motion
- [ ] #3 Slip test passes; no skipped steps over 200 cards
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Deferred: needs reflash (docs/design/up_camera.md #6, TASK-107). Keep single-axis moves (#5).
<!-- SECTION:NOTES:END -->
