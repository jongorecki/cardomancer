---
id: TASK-029
title: Up-camera calibration routine + wizard step
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - redesign
  - calibration
milestone: m-2
dependencies:
  - TASK-026
  - TASK-027
  - TASK-028
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/03_vision_identification.md
priority: high
ordinal: 29000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Pick a printed ChArUco card, servo X until centred, compute px/mm, rotation, mirror, cup offset, focus Z; save. Includes a mirror self-test.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Repeatability ±0.3 mm and ±0.5° over 5 runs
- [ ] #2 Self-test rejects a mirrored configuration
- [ ] #3 Wizard step replaces the staging ROI step
<!-- AC:END -->
