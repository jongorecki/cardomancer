---
id: TASK-001
title: Fix auto safety-reset driving Z DOWN instead of up
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - bug
  - safety
  - motion
milestone: m-0
dependencies: []
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 1000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
`_auto_safety_reset` (web_worker.py:489) calls `gcode_control.move_z(0)` under a 'Lift Z' comment, but Z=0 is fully down (gcode_control.py:55). Runs after any failed sort/probe/test-scan.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Reset moves to Z_CLEAR_HEIGHT (or z_to_top) with pumps off
- [ ] #2 Unit test with mocked gcode asserts no `G0 Z0` is emitted
- [ ] #3 Other move_z call sites audited for the same mistake
<!-- AC:END -->
