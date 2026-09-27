---
id: TASK-108
title: Explore a self-raising source magazine (spring/lift pushes the stack up)
status: To Do
assignee: []
created_date: '2026-09-27 18:55'
labels:
  - hardware
  - idea
  - perf
milestone: m-2
dependencies: []
priority: low
ordinal: 108000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Owner idea, not working yet: a magazine that keeps the top card at a fixed height, so pickup doesn't have to probe down a shrinking stack. Could shorten the Z descent at the source and make double-pick detection by stack height simpler or unnecessary.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Prototype holds the top card within a set height tolerance across a full stack
- [ ] #2 Source probe descent measured before and after
- [ ] #3 Interaction with double-pick detection (TASK-098) documented
<!-- AC:END -->
