---
id: TASK-102
title: Overlap identification compute with travel (then speculative bin commit)
status: To Do
assignee: []
created_date: '2026-09-27 18:22'
updated_date: '2026-09-27 18:47'
labels:
  - redesign
  - perf
milestone: m-2
dependencies:
  - TASK-097
  - TASK-057
  - TASK-022
  - TASK-019
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/02_server_worker_core.md
  - docs/audit/04_data_sorting_enrichment.md
  - docs/speculative_bin_commit_PLAN.md
priority: medium
ordinal: 102000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Leave the camera toward a hedged/neutral X while ID runs, commit at a checkpoint. Then speculative_bin_commit_PLAN phases 2-5 once cycle <5 s.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Only routing-relevant work blocks placement
- [ ] #2 Cycle time measured before/after
- [ ] #3 No mis-sorts from early commit over 200 cards
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Compute overlap only. Axes never move together (docs/design/up_camera.md #5).
<!-- SECTION:NOTES:END -->
