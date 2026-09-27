---
id: TASK-038
title: 'pyproject.toml with ruff, pytest-timeout, pytest-cov'
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - tooling
milestone: m-3
dependencies:
  - TASK-036
references:
  - docs/audit/06_repo_hygiene_tests.md
priority: medium
ordinal: 38000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
No Python lint config; pytest-timeout missing.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 `ruff check .` clean with baseline
- [ ] #2 `pytest --timeout=120 --cov` works
<!-- AC:END -->
