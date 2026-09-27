---
id: TASK-097
title: 'New sort cycle: pick -> image while held -> place (no staging)'
status: To Do
assignee: []
created_date: '2026-09-27 18:22'
updated_date: '2026-09-27 20:09'
labels:
  - redesign
  - worker
milestone: m-2
dependencies:
  - TASK-096
  - TASK-030
  - TASK-011
  - TASK-008
  - TASK-006
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 97000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Rewrite _cmd_detect_and_sort; port _cmd_test_scan and hardware-setup onto the same code; remove staging background/ROI/focus prompts.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Failure paths: empty head (retry, then source empty), card back (reject/flip bin), no ID (fallback)
- [ ] #2 End-to-end 100-card sort
- [ ] #3 Unit tests with mocked camera + serial
- [ ] #4 Cycle time measured vs M2 baseline
- [ ] #5 Selected by sort_cycle=upcam (default staging); the staging cycle is left intact and still passes its tests; switching back needs no code change (docs/design/up_camera.md#reversibility)
<!-- AC:END -->
