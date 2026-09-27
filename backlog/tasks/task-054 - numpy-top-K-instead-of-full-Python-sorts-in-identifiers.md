---
id: TASK-054
title: numpy top-K instead of full Python sorts in identifiers
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - perf
milestone: m-5
dependencies: []
references:
  - docs/audit/03_vision_identification.md
priority: medium
ordinal: 54000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Both identifiers sort ~62K Python tuples per call.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Same top-10 on regression set
- [ ] #2 Timing before/after
<!-- AC:END -->
