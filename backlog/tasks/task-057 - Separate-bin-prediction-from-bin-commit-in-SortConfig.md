---
id: TASK-057
title: Separate bin prediction from bin commit in SortConfig
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - sorting
  - redesign
milestone: m-5
dependencies:
  - TASK-056
references:
  - docs/audit/04_data_sorting_enrichment.md
priority: high
ordinal: 57000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
get_bin increments counts as a side effect (sort_config.py:110,130); blocks early/speculative bin decisions. Includes 'possible bins' across candidate printings.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Side-effect-free evaluate(card) + commit(bin) on physical release
- [ ] #2 evaluate twice doesn't change counts
- [ ] #3 Possible-bins API; single-bin flagged safe to commit
- [ ] #4 test_sort_config passes
<!-- AC:END -->
