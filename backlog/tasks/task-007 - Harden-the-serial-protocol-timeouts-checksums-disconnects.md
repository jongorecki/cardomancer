---
id: TASK-007
title: 'Harden the serial protocol (timeouts, checksums, disconnects)'
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - reliability
  - motion
milestone: m-0
dependencies:
  - TASK-003
references:
  - docs/audit/01_motion_hardware.md
priority: high
ordinal: 7000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Missed `ok` times out silently, so M400 can return early; M106 isn't synced with motion; worker keeps going after serial errors.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 Line numbers + checksums with resend handling
- [ ] #2 Missing ok / disconnect raises
- [ ] #3 Worker aborts the cycle on serial error; no cards recorded after a disconnect
- [ ] #4 Fake-serial unit test covers timeout path
<!-- AC:END -->
