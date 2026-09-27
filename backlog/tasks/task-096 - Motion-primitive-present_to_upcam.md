---
id: TASK-096
title: Motion primitive present_to_upcam()
status: To Do
assignee: []
created_date: '2026-09-27 18:22'
labels:
  - redesign
  - motion
milestone: m-2
dependencies:
  - TASK-028
  - TASK-042
references:
  - docs/audit/01_motion_hardware.md
priority: high
ordinal: 96000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Single sync point, optional dx offset for multi-shot. Delete staging primitives.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 G-code sequence unit test for the primitive
- [ ] #2 No staging symbols in gcode_control
<!-- AC:END -->
