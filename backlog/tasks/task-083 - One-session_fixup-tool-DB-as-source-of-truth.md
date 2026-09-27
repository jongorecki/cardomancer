---
id: TASK-083
title: One session_fixup tool; DB as source of truth
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - tooling
  - data
milestone: m-7
dependencies:
  - TASK-064
references:
  - docs/audit/04_data_sorting_enrichment.md
priority: low
ordinal: 83000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
apply_verified_corrections writes empty set_code (:77); hard-coded session ids; correct_session_csv drops columns.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Single tool updates DB and regenerates CSV
<!-- AC:END -->
