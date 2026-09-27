---
id: TASK-046
title: (Optional) Rewrite git history to drop large blobs
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - git
milestone: m-3
dependencies:
  - TASK-034
  - TASK-035
references:
  - docs/audit/06_repo_hygiene_tests.md
priority: low
ordinal: 46000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
.git is 291 MB: backup/quasai.zip (70 MB), old printings_map and staging_bg_ref versions. Force-push needs owner approval.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Pack <30 MB
- [ ] #2 Owner approved; clones re-made
<!-- AC:END -->
