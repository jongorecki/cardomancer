---
id: TASK-099
title: Re-tune all scan-tuned thresholds on up-camera data
status: To Do
assignee: []
created_date: '2026-09-27 18:22'
labels:
  - redesign
  - calibration
  - identification
milestone: m-2
dependencies:
  - TASK-097
  - TASK-043
references:
  - docs/audit/03_vision_identification.md
priority: high
ordinal: 99000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
phash 120/90, hybrid 82/85/5, DINO 0.4, card-back 100, frame/List/icon thresholds, frame template matcher.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Labelled capture set >=500 cards
- [ ] #2 Regression script reports accuracy + wrong-ID rate
- [ ] #3 Constants updated with provenance comments
<!-- AC:END -->
