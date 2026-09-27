---
id: TASK-055
title: Slim card-data artifact; load Scryfall data once
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - perf
  - memory
milestone: m-5
dependencies: []
references:
  - docs/audit/03_vision_identification.md
  - docs/audit/04_data_sorting_enrichment.md
priority: medium
ordinal: 55000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
cards.py loads a 539 MB JSON at import; parsed up to 3x; scripts re-parse it.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Parsed once per process via one index module
- [ ] #2 >5x less RAM, startup measured
- [ ] #3 reload_card_data swaps atomically
<!-- AC:END -->
