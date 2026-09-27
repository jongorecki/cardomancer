---
id: TASK-064
title: Versioned DB migrations and schema hardening
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - data
  - schema
milestone: m-5
dependencies: []
references:
  - docs/audit/04_data_sorting_enrichment.md
priority: medium
ordinal: 64000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
PRAGMA user_version + ordered migrations for both DBs; unique index on inventory(name,set_code,collector_number); assign_box split keeps foil_quantity/divider_id.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 No DDL on every connect
- [ ] #2 Dedupe migration
- [ ] #3 Scan instrumentation columns
<!-- AC:END -->
