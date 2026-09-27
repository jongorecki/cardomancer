---
id: TASK-078
title: Legal review and scraper gating
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - legal
  - enrichment
milestone: m-7
dependencies: []
references:
  - docs/audit/04_data_sorting_enrichment.md
  - docs/audit/07_docs_todo_mining.md
priority: medium
ordinal: 78000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
scrape_tagger_descriptions.py bypasses a login wall; CK/Moxfield/EDHREC scrapers risky for a paid product. See plans/legal_prep.md.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Per-source enabled flag, risky ones default off in distributed builds
- [ ] #2 Tagger scraper removed or personal-only
- [ ] #3 Spellbook via bulk export
- [ ] #4 'Unofficial / not affiliated with Wizards' disclaimer
- [ ] #5 Answers to lawyer questions
<!-- AC:END -->
