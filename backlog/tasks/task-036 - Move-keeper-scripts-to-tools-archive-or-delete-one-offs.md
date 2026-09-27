---
id: TASK-036
title: 'Move keeper scripts to tools/, archive or delete one-offs'
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - hygiene
  - refactor
milestone: m-3
dependencies:
  - TASK-012
references:
  - docs/audit/06_repo_hygiene_tests.md
priority: medium
ordinal: 36000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
~75 loose root scripts: ~25 to tools/{data,foil,bench,eval,set_symbol,hw}, ~30 to archive/ (with README index), ~20 deleted. 16bit_rgb_create_card_hashes.py and create_card_hashes_v2.py are imported by web_database.py — shim or retire them first.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 No diag/diagnose/prototype/probe files at root
- [ ] #2 Moved tools run with --help
- [ ] #3 archive/README.md index
- [ ] #4 pytest green
<!-- AC:END -->
