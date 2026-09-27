---
id: TASK-001
title: Fix auto safety-reset driving Z DOWN instead of up
status: Done
assignee:
  - '@claude'
created_date: '2026-09-27 18:19'
updated_date: '2026-09-27 19:06'
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
- [x] #1 Reset moves to Z_CLEAR_HEIGHT (or z_to_top) with pumps off
- [x] #2 Unit test with mocked gcode asserts no `G0 Z0` is emitted
- [x] #3 Other move_z call sites audited for the same mistake
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. Change _auto_safety_reset to move_z(Z_CLEAR_HEIGHT). 2. Regression test with mocked gcode. 3. Audit other move_z callers.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Only other move_z caller is _cmd_move_z (explicit user-requested Z), which is correct. tests/test_safety_quick.py fails on the old code and passes now; full suite 1332 passed / 1 skipped.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
_auto_safety_reset now lifts to Z_CLEAR_HEIGHT instead of Z=0 (fully down). Verified by tests/test_safety_quick.py (fails on old code) and the full pytest suite.
<!-- SECTION:FINAL_SUMMARY:END -->
