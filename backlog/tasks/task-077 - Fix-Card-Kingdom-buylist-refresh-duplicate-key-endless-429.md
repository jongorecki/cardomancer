---
id: TASK-077
title: Fix Card Kingdom buylist refresh (duplicate key + endless 429)
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - bug
  - enrichment
milestone: m-7
dependencies: []
references:
  - docs/audit/04_data_sorting_enrichment.md
priority: high
ordinal: 77000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
buylist_ck.py:155/:416 insert per printing into per-card key; :327 retries 429 forever.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Rows merged per card by documented rule
- [ ] #2 Capped exponential backoff; partial fetch marked failed
- [ ] #3 Test with two printings
<!-- AC:END -->
