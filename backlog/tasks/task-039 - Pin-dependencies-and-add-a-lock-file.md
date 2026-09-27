---
id: TASK-039
title: Pin dependencies and add a lock file
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - deps
milestone: m-3
dependencies: []
references:
  - docs/audit/06_repo_hygiene_tests.md
priority: medium
ordinal: 39000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Flask, flask_socketio, opencv, numpy, pillow, imagehash, pyserial unpinned; torch install undocumented.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 requirements-lock.txt
- [ ] #2 Clean venv install from docs passes the suite
<!-- AC:END -->
