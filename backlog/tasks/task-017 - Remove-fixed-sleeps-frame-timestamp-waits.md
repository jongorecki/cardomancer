---
id: TASK-017
title: Remove fixed sleeps; frame-timestamp waits
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - perf
  - camera
milestone: m-1
dependencies:
  - TASK-014
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 17000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
~3.3 s/card in fixed sleeps/dwells. Capture loop sleeps 30 ms; get_sharp_frame counts polls not new frames.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Frames carry sequence+timestamp; get_frame_after(t)
- [ ] #2 Fixed exposure; continuous_delay default 0
- [ ] #3 PRESSURE_ON_MS / VACUUM_ON_DELAY_MS bench-tested
- [ ] #4 >=2 s saved per card, no rise in no-detects over 200 cards
<!-- AC:END -->
