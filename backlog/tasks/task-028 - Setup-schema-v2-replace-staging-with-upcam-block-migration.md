---
id: TASK-028
title: 'Setup schema v2: replace staging with upcam block + migration'
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
updated_date: '2026-09-27 20:09'
labels:
  - redesign
  - config
milestone: m-2
dependencies:
  - TASK-024
references:
  - docs/audit/01_motion_hardware.md
priority: high
ordinal: 28000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
`upcam{x, z_image, px_per_mm, rotation, flip, crop_quad}` in _last_setup/setup_*/bin_configs.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 v1 files still load (staging ignored)
- [ ] #2 Schema versioned and tested
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Reversibility: keep the staging block alongside upcam (don't drop it on migration).
<!-- SECTION:NOTES:END -->
