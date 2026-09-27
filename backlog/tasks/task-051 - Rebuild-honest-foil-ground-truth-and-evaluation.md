---
id: TASK-051
title: Rebuild honest foil ground truth and evaluation
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - foil
  - data
milestone: m-4
dependencies: []
references:
  - docs/audit/03_vision_identification.md
priority: high
ordinal: 51000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Session-59 labels are the detector's own output (_foil_tune.py:204-225) and scores are on training data.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Human-verified label file with provenance (old frames, etched, full-art)
- [ ] #2 Held-out precision/recall at realistic prevalence
- [ ] #3 Current model re-scored
<!-- AC:END -->
