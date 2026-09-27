---
id: TASK-049
title: 'Set icon: never commit when a candidate lacks a template; build all templates'
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - bug
  - accuracy
milestone: m-4
dependencies: []
references:
  - docs/audit/03_vision_identification.md
priority: high
ordinal: 49000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
set_icon.py skips candidates with no template and can commit the wrong set (wrong price).
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Missing-template candidate yields no commit (test)
- [ ] #2 Template coverage >=95% of paper sets
<!-- AC:END -->
