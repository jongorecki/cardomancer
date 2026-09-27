---
id: TASK-101
title: Foil detection with two held-card capture positions on the up-camera
status: To Do
assignee: []
created_date: '2026-09-27 18:22'
updated_date: '2026-09-27 18:47'
labels:
  - redesign
  - foil
  - vision
milestone: m-2
dependencies:
  - TASK-026
  - TASK-029
  - TASK-051
references:
  - docs/audit/03_vision_identification.md
  - docs/audit/01_motion_hardware.md
priority: medium
ordinal: 101000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Preferred: 2-4 strobed LEDs at different angles (optionally cross-polarised), one frame per light, card still; no reference image. Fallback: multi-frame while moving (A/B against it).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Two frames at a small X shift while the card is held (sign and px/mm correct)
- [ ] #2 Scored against the rebuilt ground truth (held-out precision/recall reported)
- [ ] #3 Added cycle time measured
- [ ] #4 Decision recorded: keep, tune, or revisit multi-LED
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Owner decision: two positions first (docs/design/up_camera.md #3).
<!-- SECTION:NOTES:END -->
