---
id: TASK-033
title: Remove stale worktrees and merged branches
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - hygiene
  - git
milestone: m-3
dependencies: []
references:
  - docs/audit/06_repo_hygiene_tests.md
priority: high
ordinal: 33000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
3 .claude/worktrees entries, all fully merged into main.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 git worktree list shows only main
- [ ] #2 Branches deleted
- [ ] #3 git gc run
<!-- AC:END -->
