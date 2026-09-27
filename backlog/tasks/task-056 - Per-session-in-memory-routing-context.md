---
id: TASK-056
title: Per-session in-memory routing context
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - perf
  - sorting
milestone: m-5
dependencies: []
references:
  - docs/audit/04_data_sorting_enrichment.md
priority: high
ordinal: 56000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Per-card path opens SQLite, runs DDL, parses deck JSON (moxfield.py:673), may hit the otag API.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 No SQLite/DDL/JSON parse per card
- [ ] #2 Routing <1 ms p99 with enrichment tokens
- [ ] #3 fetch_otag_data local-first with timeout + UA
<!-- AC:END -->
