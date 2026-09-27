---
id: TASK-018
title: Make the foil B-frame capture opt-in (default off)
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - perf
  - foil
milestone: m-1
dependencies:
  - TASK-014
references:
  - docs/audit/03_vision_identification.md
priority: high
ordinal: 18000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The second foil frame costs ~1 s/card (extra X move + sleeps) and nothing scores it in production.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Flag off: no extra move/sleep per card
- [ ] #2 Flag on: behaviour unchanged
- [ ] #3 Measured cycle drop logged
<!-- AC:END -->
