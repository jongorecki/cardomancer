---
id: TASK-100
title: 'UI: remove staging, support two cameras, rework calibration wizard'
status: To Do
assignee: []
created_date: '2026-09-27 18:22'
labels:
  - redesign
  - frontend
milestone: m-2
dependencies:
  - TASK-027
  - TASK-029
  - TASK-097
references:
  - docs/audit/05_frontend.md
  - docs/audit/01_motion_hardware.md
priority: high
ordinal: 100000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
~110 staging references across static/ and templates/ (audit 05 §4.5). Feed manager starts/stops MJPEG by visibility (also fixes streams left running).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 No staging / marker 49 / expect_staging / set-staging-roi left in UI
- [ ] #2 Per-camera feeds + two health badges
- [ ] #3 Running stage shows up-camera with recognition overlay
- [ ] #4 Wizard completes with no staging steps; defaults match Setup tab
- [ ] #5 motion.js canvas draws the up-camera
<!-- AC:END -->
