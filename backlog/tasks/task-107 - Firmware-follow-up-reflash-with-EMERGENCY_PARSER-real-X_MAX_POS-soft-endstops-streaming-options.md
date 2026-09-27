---
id: TASK-107
title: >-
  Firmware follow-up: reflash with EMERGENCY_PARSER, real X_MAX_POS, soft
  endstops, streaming options
status: To Do
assignee: []
created_date: '2026-09-27 18:47'
labels:
  - firmware
  - firmware-deferred
  - safety
milestone: m-0
dependencies:
  - TASK-002
priority: low
ordinal: 107000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Deferred until the owner chooses to reflash. Collects the firmware-only parts of TASK-003, TASK-005 and TASK-022.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Owner approves a reflash
- [ ] #2 EMERGENCY_PARSER on; X_MAX_POS = real rail; M211 S0 removed from the host
- [ ] #3 Config committed under firmware/
<!-- AC:END -->
