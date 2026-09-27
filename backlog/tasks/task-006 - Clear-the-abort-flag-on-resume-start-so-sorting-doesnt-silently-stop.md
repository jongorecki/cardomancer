---
id: TASK-006
title: Clear the abort flag on resume/start so sorting doesn't silently stop
status: In Progress
assignee:
  - '@claude'
created_date: '2026-09-27 18:19'
updated_date: '2026-09-27 19:06'
labels:
  - bug
  - worker
milestone: m-0
dependencies: []
references:
  - docs/audit/02_server_worker_core.md
priority: high
ordinal: 6000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
`_abort_requested` survives resume, session start and cancel; after a camera auto-pause or stray calibration Cancel every card cycle exits immediately.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 resume/start_session/start_continuous/detect clear the flag
- [x] #2 Test: camera auto-pause then Resume keeps sorting
- [ ] #3 /api/calibration/cancel only sets the flag while calibration is running
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
1. _clear_abort() in _cmd_resume, _cmd_start_session, _cmd_start_continuous (not in detect_and_sort, so a stop between continuous cycles is not lost). 2. /api/calibration/cancel only calls request_abort when no session is sorting/paused. 3. Tests.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
Done: resume, start_session and start_continuous clear the flag; tests cover resume after camera auto-pause and continuous start. Deliberately NOT cleared in detect_and_sort: continuous sort re-enqueues it, so clearing there would swallow a stop request between cards. Calibration-cancel gating (skip abort while sorting/paused) is implemented but has no automated test yet; needs a web_server test or a manual check.
<!-- SECTION:NOTES:END -->
