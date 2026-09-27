---
id: TASK-003
title: Make E-stop reliable (host side) and make Reset & Re-home work
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
updated_date: '2026-09-27 18:47'
labels:
  - safety
  - firmware
  - bug
milestone: m-0
dependencies:
  - TASK-002
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 3000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
M112 is written from a Flask thread with no serial lock, EMERGENCY_PARSER is off, and M112 kill() can't be recovered by M999 (likely cause of the 'reset doesn't resume' bug). Also: Escape key currently fires e-stop (see UI task).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 All serial writes go through one lock; e-stop takes priority and stops streaming further commands
- [ ] #2 Evaluate an immediate host-side stop without firmware changes (e.g. serial DTR reset of the board) and document the measured latency
- [ ] #3 Recovery reconnects the board instead of relying on M999; Reset & Re-home resumes a paused session (test)
- [ ] #4 Recommend a hardware e-stop that cuts motor + pump power
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Firmware is off-limits for now (docs/design/up_camera.md #6). Without EMERGENCY_PARSER, M112/M410 only run after the current buffered move; enabling it is in TASK-107.
<!-- SECTION:NOTES:END -->
