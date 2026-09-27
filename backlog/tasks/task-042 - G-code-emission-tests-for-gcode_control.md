---
id: TASK-042
title: G-code emission tests for gcode_control
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - tests
  - motion
milestone: m-3
dependencies: []
references:
  - docs/audit/06_repo_hygiene_tests.md
priority: high
ordinal: 42000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
gcode_control is only tested through mocks at call sites.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Fake serial asserts command strings for moves, homing, vacuum, probe, e-stop
- [ ] #2 Every public motion method covered
<!-- AC:END -->
