---
id: TASK-027
title: Multi-camera registry in web_camera
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-27 18:20'
updated_date: '2026-09-27 19:24'
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
- [x] #2 /api/camera/*?cam= and per-camera health
- [x] #3 Auto-pause only for the up camera
- [ ] #4 Worker references cameras by role
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. web_camera: CameraManager takes role + per-camera settings (device index or name, rotate, flip, width/height/fps, exposure/WB/focus controls). Controls re-applied on every open/reconnect.
2. Registry: roles 'down' (existing carriage camera) and 'up' (new AC410). Config in machine-local camera_config.json (gitignored) + CARDOMANCER_CAM_<ROLE>_* env overrides. 'up' disabled until configured. ID_ROLE setting ('down' until the new sort cycle TASK-097 lands). Module-level camera stays = down for back-compat.
3. Device-name selection via optional pygrabber (DirectShow enumeration); falls back to index.
4. web_server: camera routes take ?cam=<role> (default id role); /api/cameras lists all; health listener per camera, auto-pause only for the ID camera; save-settings endpoint persists per-role settings.
5. Worker: every camera lookup goes through web_camera.id_camera().
6. Tests: registry/config loading, settings re-apply on open (mocked cv2), per-role routing, auto-pause only for ID role. Full suite.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Implemented: CameraManager(role, device_name, rotate, flip, width/height/fps, controls, enabled); controls re-applied on every open (focus lock kept across reconnect). Registry in web_camera: cameras{down,up}, get_camera/id_camera/id_role, camera_config.json (gitignored) + CARDOMANCER_CAM_* env. 'up' defaults disabled, 1080p60. Server: ?cam=<role> on /api/camera/* (default down), GET /api/cameras, POST /api/cameras/<role>; session preflight checks the ID camera; health listener extracted to _install_camera_health_listeners(), auto-pause only for the ID role. Worker camera lookups go through id_camera() (ID role stays 'down' until TASK-097). Tests: tests/test_multi_camera.py (16); full suite 1348 passed / 1 skipped.
Open: AC1 needs both cameras attached (hardware). AC4 is implemented but only covered indirectly. Device-name selection needs optional pygrabber (not added to requirements). UI still shows one camera (TASK-100).
<!-- SECTION:NOTES:END -->
