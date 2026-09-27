---
id: TASK-037
title: Reconcile/delete root test_* scripts
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - tests
milestone: m-3
dependencies:
  - TASK-036
references:
  - docs/audit/06_repo_hygiene_tests.md
  - docs/audit/04_data_sorting_enrichment.md
priority: low
ordinal: 37000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
31 root test_*.py are not in the pytest suite; test_10_cards.py and test_full_sort.py import removed sorting.get_bin_number.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Missing cases ported to tests/
- [ ] #2 Hardware scripts moved to tools/hw or deleted
- [ ] #3 No broken imports
<!-- AC:END -->
