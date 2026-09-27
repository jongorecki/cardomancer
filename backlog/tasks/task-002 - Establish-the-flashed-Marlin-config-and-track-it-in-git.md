---
id: TASK-002
title: Establish the flashed Marlin config and track it in git
status: To Do
assignee: []
created_date: '2026-09-27 18:19'
labels:
  - firmware
  - docs
milestone: m-0
dependencies: []
references:
  - docs/audit/01_motion_hardware.md
  - docs/audit/06_repo_hygiene_tests.md
priority: high
ordinal: 2000
---

## Description

<!-- SECTION:DESCRIPTION:BEGIN -->
Two disagreeing config pairs exist outside git: D:/Card_Sorter/Configuration*.h (G38 off, accel 500) and Marlin-2.1.3-b1/Marlin/Configuration*.h (G38 on, accel 1500/1000). EEPROM is on, so only M503 tells the truth.
<!-- SECTION:DESCRIPTION:END -->

## Acceptance Criteria
<!-- AC:BEGIN -->
- [ ] #1 M115/M503 dump captured (benchmark_motion.py --info) and committed
- [ ] #2 Single source-of-truth Configuration pair committed under firmware/ with board + Marlin version + flash instructions
- [ ] #3 cycle_time_findings Findings 1 & 3 corrected
<!-- AC:END -->
