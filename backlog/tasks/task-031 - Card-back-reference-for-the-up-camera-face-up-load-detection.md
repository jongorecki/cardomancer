---
id: TASK-031
title: Face-up-load detection and orientation handling on the up-camera
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
updated_date: '2026-09-27 18:47'
labels:
  - redesign
  - vision
milestone: m-2
dependencies:
  - TASK-027
  - TASK-030
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/07_docs_todo_mining.md
priority: medium
ordinal: 31000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Recapture card_back_reference.png via a web flow (replace capture_card_back_ref.py); repurpose the check as 'card loaded face-up' detection.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Web capture flow
- [ ] #2 0/180° handling verified on held images
- [ ] #3 No regression on misread cases
<!-- AC:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Stacks may be rotated 180 and occasional cards face-up. DFC back faces are NOT card backs; identification must accept either face.
<!-- SECTION:NOTES:END -->
