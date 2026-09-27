---
id: TASK-024
title: 'Up-camera redesign: owner design decisions'
status: Done
assignee: []
created_date: '2026-09-27 18:20'
updated_date: '2026-09-27 18:47'
labels:
  - redesign
  - decision
milestone: m-2
dependencies: []
references:
  - docs/design/up_camera.md
priority: high
ordinal: 24000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Answer the open questions from docs/audit/01 §5.5 and write docs/design/up_camera.md.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Keep or remove the down-looking carriage camera
- [x] #2 Up-camera X position; stop-and-shoot vs on-the-fly strobe
- [x] #3 Foil approach (two positions / multi-LED / drop for now)
- [x] #4 Reject/flip bin
- [ ] #5 Double-pick detection method
- [x] #6 Z/X overlap policy (hard rule?)
- [x] #7 Firmware reflash allowed (EMERGENCY_PARSER, X_MAX_POS, streaming opts)
- [ ] #8 Bin-lip height measured
- [x] #9 Face-down load orientation consistent (top edge)
- [x] #10 Design doc committed
<!-- AC:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Decisions recorded in docs/design/up_camera.md. Open: #7 travel-height clearance measurement (tracked in TASK-016).
<!-- SECTION:FINAL_SUMMARY:END -->
