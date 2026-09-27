---
id: TASK-103
title: Delete legacy staging artifacts and update plans
status: To Do
assignee: []
created_date: '2026-09-27 18:22'
labels:
  - redesign
  - cleanup
  - docs
milestone: m-2
dependencies:
  - TASK-097
  - TASK-100
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/02_server_worker_core.md
  - docs/audit/07_docs_todo_mining.md
priority: low
ordinal: 103000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
calibrate_staging.py, calibrate_camera_height.py, generate_staging_mat.py, card_back_id.py, staging_*.json, staging_bg_ref.png, bounding_box.json, marker 49, staging endpoints/events/preflight, _staging_snapshot_frame.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 No staging code/config remains
- [ ] #2 sort_flow_stages, z_bounce_retry, autonomy_ladder, cycle-time docs updated
<!-- AC:END -->
