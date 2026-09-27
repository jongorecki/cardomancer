---
id: TASK-052
title: Make DINO scores comparable to phash thresholds
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - accuracy
  - identification
milestone: m-4
dependencies:
  - TASK-019
references:
  - docs/audit/03_vision_identification.md
priority: medium
ordinal: 52000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
DINO wins return a synthetic (1-sim)*200 distance judged by phash thresholds.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Method + native score carried to worker
- [ ] #2 Per-method accept/low-confidence thresholds
- [ ] #3 _names_match exact (with face splitting)
<!-- AC:END -->
