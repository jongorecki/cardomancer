---
id: TASK-030
title: Held-card detector (fixed pose + refinement)
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - redesign
  - vision
milestone: m-2
dependencies:
  - TASK-029
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/03_vision_identification.md
  - docs/audit/07_docs_todo_mining.md
priority: high
ordinal: 30000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Warp the calibrated fixed crop with edge refinement against the dark backdrop to canonical 745x1040; orientation from known load direction. Consider card_art_id_handoff/reference/detect.py.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 >=99% detection on a corpus of held-card frames (>=500 for final)
- [ ] #2 <60 ms per frame
- [ ] #3 Golden-image tests
<!-- AC:END -->
