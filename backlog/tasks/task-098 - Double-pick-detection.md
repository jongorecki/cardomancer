---
id: TASK-098
title: Double-pick detection
status: To Do
assignee: []
created_date: '2026-09-27 18:22'
updated_date: '2026-09-27 18:47'
labels:
  - redesign
  - reliability
milestone: m-2
dependencies:
  - TASK-097
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/07_docs_todo_mining.md
priority: high
ordinal: 98000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
The up-camera sees the bottom card of a stuck pair and silently mis-sorts it. Cheapest: stack-top Z delta across consecutive source probes; optional vacuum sensor. On suspicion: reject bin + Z-bounce retry.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 >=95% of deliberate stuck pairs caught
- [ ] #2 <1% false positives
- [ ] #3 Z-bounce retry validated live
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
No vacuum sensor or firmware changes. First candidate: stack-top Z delta between consecutive source probes. Suspects go to the preset fallback bin.
<!-- SECTION:NOTES:END -->
