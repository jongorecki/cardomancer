---
id: TASK-097.01
title: 'sort_cycle switch: staging vs upcam, selectable without code changes'
status: Done
assignee:
  - '@claude'
created_date: '2026-09-27 20:22'
updated_date: '2026-09-27 20:28'
labels:
  - redesign
  - worker
  - config
milestone: m-2
dependencies: []
parent_task_id: TASK-097
priority: high
ordinal: 111000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Infrastructure for docs/design/up_camera.md#reversibility: a persisted sort_cycle setting (staging default | upcam), locked in at session start, dispatching the per-card cycle. The upcam cycle itself is TASK-097; until then it stops safely.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [x] #1 sort_cycle persisted in gitignored machine_settings.json, env override CARDOMANCER_SORT_CYCLE, default staging
- [x] #2 Cycle is captured at session start; changing it is refused while a session is sorting or paused
- [x] #3 detect_and_sort dispatches on the session's cycle; staging body unchanged
- [x] #4 upcam before TASK-097 exists pauses the session with a clear message and no motion
- [x] #5 Session start refuses upcam unless the ID camera role is 'up'
- [x] #6 GET/POST /api/machine/sort-cycle and a selector on the Setup tab
- [x] #7 Tests; full suite passes
<!-- AC:END -->

## Implementation Plan

<!-- SECTION:PLAN:BEGIN -->
machine_settings.py (sort_cycle, gitignored json, env override) -> worker captures self.sort_cycle at start_session (staging background capture only for staging) -> _cmd_detect_and_sort dispatches upcam to _detect_and_sort_upcam (pauses, no motion) -> server: session-start preflight (upcam needs id_role up; staging preflight only for staging), GET/POST /api/machine/sort-cycle, POST /api/camera/id-role, both refused mid-session -> Setup tab selectors.
<!-- SECTION:PLAN:END -->

## Implementation Notes

<!-- SECTION:NOTES:BEGIN -->
tests/test_sort_cycle.py (10) + full suite 1358 passed / 1 skipped. Headless UI check: switching cycle and ID camera updates the server settings, mismatch hint shows and clears, no page errors. Staging cycle body is untouched apart from the dispatch line at the top.
<!-- SECTION:NOTES:END -->

## Final Summary

<!-- SECTION:FINAL_SUMMARY:BEGIN -->
Added a persisted sort_cycle switch (staging default | upcam) plus an ID-camera selector, fixed per session and exposed on the Setup tab. upcam currently pauses safely until TASK-097 builds the cycle. Verified by unit/API tests, the full suite and a headless browser check.
<!-- SECTION:FINAL_SUMMARY:END -->
