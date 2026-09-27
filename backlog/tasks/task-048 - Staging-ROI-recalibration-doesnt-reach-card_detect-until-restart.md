---
id: TASK-048
title: Staging ROI recalibration doesn't reach card_detect until restart
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - bug
  - vision
milestone: m-4
dependencies: []
references:
  - docs/audit/03_vision_identification.md
priority: high
ordinal: 48000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
save_staging_roi clears detection.py's cache but not card_detect's (invalidate_staging_roi has no callers). Superseded by redesign but bites today.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Next detection after ROI change uses new limits/px-per-mm
- [ ] #2 Unit test
<!-- AC:END -->
