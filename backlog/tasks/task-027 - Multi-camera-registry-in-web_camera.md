---
id: TASK-027
title: Multi-camera registry in web_camera
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - redesign
  - camera
  - backend
milestone: m-2
dependencies:
  - TASK-024
  - TASK-017
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 27000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Role-named cameras (up/down) selected by device name; per-role rotation/flip, resolution, manual exposure/WB/focus persisted and re-applied on reconnect.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Both cameras stream simultaneously
- [ ] #2 /api/camera/*?cam= and per-camera health
- [ ] #3 Auto-pause only for the up camera
- [ ] #4 Worker references cameras by role
<!-- AC:END -->
