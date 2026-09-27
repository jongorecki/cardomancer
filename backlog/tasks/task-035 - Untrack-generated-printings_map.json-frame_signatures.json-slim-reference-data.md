---
id: TASK-035
title: >-
  Untrack generated printings_map.json / frame_signatures.json; slim reference
  data
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - hygiene
  - data
milestone: m-3
dependencies:
  - TASK-034
references:
  - docs/audit/06_repo_hygiene_tests.md
  - docs/audit/03_vision_identification.md
priority: medium
ordinal: 35000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
printings_map.json 45 MB tracked; frame_signatures duplicated; 3 dead card_hashes*.json; ~4.7 GB of old default-cards dumps locally; v3 JSON holds 6x the hashes used.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Documented regenerate command for each
- [ ] #2 build_hash_db_v3 writes only Region A to npz/.bin
- [ ] #3 Dead hash JSONs and old dumps removed
- [ ] #4 DB manifests + ID-consistency check at load
<!-- AC:END -->
