---
id: TASK-010
title: Fix wishlist bin never firing
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - bug
  - sorting
milestone: m-0
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
  - docs/audit/04_data_sorting_enrichment.md
priority: medium
ordinal: 10000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
web_worker.py:1898 passes `set_code` to collection_db.check_wishlist_match (:1456) which doesn't accept it; exception is swallowed on every card.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Signature and call agree
- [ ] #2 Wishlist cached in memory, not queried per card
- [ ] #3 Test: matching and non-matching card
<!-- AC:END -->
