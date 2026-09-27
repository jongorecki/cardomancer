---
id: TASK-021
title: Move per-card I/O off the critical path
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - perf
  - persistence
milestone: m-1
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
priority: medium
ordinal: 21000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Image writes, bins.json rewrites, per-card DB connect + DDL, full session_stats payloads.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Background image writer
- [ ] #2 bins.json written periodically/at end
- [ ] #3 One long-lived DB connection; DDL once per process
- [ ] #4 session_stats sends deltas
<!-- AC:END -->
