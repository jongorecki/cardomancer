---
id: TASK-070
title: Close unescaped innerHTML gaps
status: To Do
assignee: []
created_date: '2026-09-27 18:21'
labels:
  - frontend
  - security
milestone: m-6
dependencies: []
references:
  - docs/audit/05_frontend.md
priority: medium
ordinal: 70000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Three escapeHtml variants; many unescaped sinks; card names in inline onclick.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 One helper
- [ ] #2 All listed sinks escaped or textContent
- [ ] #3 Card named <b>"x"&</b> renders literally everywhere
<!-- AC:END -->
