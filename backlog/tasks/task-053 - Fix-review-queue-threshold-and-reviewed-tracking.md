---
id: TASK-053
title: Fix review queue threshold and reviewed tracking
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - bug
  - review
milestone: m-4
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
priority: medium
ordinal: 53000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Default threshold not on the 256-bit scale; reviewing overwrites hash_distance with 0; resumed-session crops don't resolve.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Threshold derived from IDENTITY_LOW_CONFIDENCE_DISTANCE
- [ ] #2 `reviewed` column
- [ ] #3 Resumed crops resolve
<!-- AC:END -->
