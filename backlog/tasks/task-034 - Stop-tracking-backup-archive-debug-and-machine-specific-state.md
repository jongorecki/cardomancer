---
id: TASK-034
title: 'Stop tracking backup/, archive/debug and machine-specific state'
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - hygiene
  - git
  - config
milestone: m-3
dependencies: []
references:
  - docs/audit/06_repo_hygiene_tests.md
priority: high
ordinal: 34000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
git rm --cached backup/**, archive/debug/*, staging_bg_ref.png, staging_*.json, _last_setup.json, setup_*.json; add .example templates.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Fresh clone starts with defaults
- [ ] #2 .gitignore covers them
- [ ] #3 README corrected
<!-- AC:END -->
