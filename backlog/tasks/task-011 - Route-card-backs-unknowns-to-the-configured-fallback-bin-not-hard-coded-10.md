---
id: TASK-011
title: 'Route card backs/unknowns to the configured fallback bin, not hard-coded 10'
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
updated_date: '2026-09-27 18:47'
labels:
  - bug
  - sorting
milestone: m-0
dependencies: []
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/02_server_worker_core.md
priority: medium
ordinal: 11000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
web_worker.py:1798 and :2007 hard-code logical bin 10.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 fallback_bin (or a configured reject bin) used
- [ ] #2 Test with a 7-bin config
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Decision: errors (card backs, unknowns, double picks) go to the preset fallback bin (docs/design/up_camera.md #4).
<!-- SECTION:NOTES:END -->
