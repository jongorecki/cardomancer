---
id: TASK-100.01
title: Camera settings panel in the Setup tab (per-camera config + live preview)
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-27 19:56'
updated_date: '2026-09-27 20:00'
labels:
  - frontend
  - redesign
  - camera
milestone: m-2
dependencies:
  - TASK-027
parent_task_id: TASK-100
priority: high
ordinal: 109000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Replace the single-camera 'Camera' card on the Setup tab with a panel for both roles (down, up), driving GET /api/cameras and POST /api/cameras/<role> from TASK-027.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 Each camera shows role, health, fps, and whether it is the ID camera
- [x] #2 Enable, device (by name when available, else index), rotation, mirror, and mode (1080p30/1080p60/4K30) can be changed and saved
- [ ] #3 Exposure, white balance and focus: auto toggle plus manual value, applied live and saved
- [ ] #4 Start/stop and a live preview per camera; previews stop when hidden
- [x] #5 Errors from the API are shown to the user
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
New static/modules/cameraSettings.js renders one card per role from GET /api/cameras into #camera-settings-panel (replaces the old Setup 'Camera' card and app.js status poll). Save -> POST /api/cameras/<role>; controls debounce-post live. Status polled every 2 s only while the panel is visible; previews torn down when hidden.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Verified headless (Playwright, server with CARD_SORTER_NO_AUTO_CONNECT=1, no cameras opened): both cards render, rotation/mirror/mode/manual exposure saved and re-read after reload, starting a disabled camera shows the error, no page errors. Server now returns rotate_degrees/flip_name in status and error+message on 400/404. Not verified without hardware: controls actually changing the image, live preview, stop-when-hidden with real streams (AC3/AC4).
<!-- SECTION:NOTES:END -->
