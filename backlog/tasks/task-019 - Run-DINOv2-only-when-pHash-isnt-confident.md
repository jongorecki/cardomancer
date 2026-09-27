---
id: TASK-019
title: Run DINOv2 only when pHash isn't confident
status: To Do
assignee: []
created_date: '2026-09-27 18:20'
labels:
  - perf
  - identification
milestone: m-1
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
  - docs/audit/03_vision_identification.md
priority: high
ordinal: 19000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
DINOv2 ViT-B/14 runs on every card, even when pHash already decides.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 DINO only when pHash fails <=82/gap rules, and only on the chosen orientation
- [ ] #2 362-scan regression accuracy unchanged
- [ ] #3 Per-card ID latency logged and median reduced
<!-- AC:END -->
