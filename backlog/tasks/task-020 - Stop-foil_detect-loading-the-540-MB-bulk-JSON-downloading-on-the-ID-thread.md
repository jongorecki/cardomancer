---
id: TASK-020
title: Stop foil_detect loading the 540 MB bulk JSON / downloading on the ID thread
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - perf
  - bug
  - foil
milestone: m-1
dependencies: []
references:
  - docs/audit/03_vision_identification.md
priority: high
ordinal: 20000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Missing reference image triggers a full bulk JSON load plus HTTP fetch inside the per-card ID thread.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Missing ref returns no_reference and queues a background fetch
- [ ] #2 Uses existing cards index for URLs
- [ ] #3 Test covers missing-reference path
<!-- AC:END -->
