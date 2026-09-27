---
id: TASK-014
title: Per-card cycle-time instrumentation (baseline for every speed task)
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - perf
  - observability
  - motion
milestone: m-1
dependencies: []
references:
  - docs/audit/01_motion_hardware.md
  - docs/speculative_bin_commit_PLAN.md
priority: high
ordinal: 14000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Timestamp each phase (pick, lift, X travel, capture, ID, drop) per card. Phase 1 of speculative_bin_commit_PLAN.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Per-phase CSV written per session
- [ ] #2 Report of per-phase breakdown for a session
- [ ] #3 benchmark_motion's cycle mirrors the real primitives
- [ ] #4 Baseline recorded on current hardware before other M2 tasks land
<!-- AC:END -->
