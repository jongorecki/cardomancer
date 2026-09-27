# Up-camera redesign — design decisions

Status: decided 2026-09-27 (TASK-024). Background: [docs/audit/01_motion_hardware.md](../audit/01_motion_hardware.md) §5.

## The change

Cards are loaded face-down. The head picks the top card, moves it over a new upward-facing camera, stops, and takes the image(s) while holding the card. It then carries the card straight to its bin. The staging platform, and with it the put-down / move-away / re-pick steps, goes away.

## Decisions

| # | Question | Decision |
|---|---|---|
| 1 | Keep the down-looking carriage camera? | **Keep it.** It stays available for bin calibration and checks. The up camera handles card identification. |
| 2 | Stop over the camera, or capture on the fly? | **Stop.** Detection takes about 0.5 s on the old camera. The new camera runs 4K30 or 1080p60, and we choose the mode by measurement (1080p60 gives lower frame latency; 4K gives more pixels for the set symbol and List stamp). |
| 3 | Foil approach | **Two capture positions** (a small X shift while the card is held), using the new camera. Re-evaluate after collecting data. Multi-LED lighting is a future option, not planned. |
| 4 | Where errors go | **The preset's existing `fallback` bin**, used for card backs, unrecognized cards and suspected double picks. Note: that bin also receives valid cards that match no rule. A dedicated error bin can be added later if that mix becomes a problem. |
| 5 | Can Z and X move at the same time? | **No. Hard rule.** Overlapping motion knocks bins over, so every move stays single-axis and finishes before the next axis moves. Speed work must respect this. "Overlap" in the backlog only means running computation (identification, bin choice) while the machine moves. It never means moving two axes at once. |
| 6 | Firmware changes | **Not now.** Nothing in the current plan may require reflashing the board. Reading the config over serial (M115/M503) is fine. Firmware-dependent tasks carry the `firmware-deferred` label. |
| 7 | Travel-height clearance | **Keep Z=200 for now.** It is roughly the clearance needed over the bins and other obstacles, plus a small margin. Revisit only after the up-camera is integrated (TASK-016, low priority). |
| 8 | Card orientation | **Unknown and mixed.** Cards may arrive rotated 180° (the detector already tries 0°/180°), and a card may occasionally be face-up (the camera then sees a card back, and it goes to the fallback bin). For double-faced cards (DFCs), either face may be the one facing the camera, and identification must accept either face as a match. A DFC's back face is not a card back. |

## Consequences for the backlog

- Firmware-dependent parts of the E-stop, soft-endstop and streamed-motion tasks are deferred. Host-side parts continue.
- Foil work (TASK-101) is scoped to the two-position approach first.
- Double-pick detection (TASK-098) must not rely on a vacuum sensor or firmware changes. The first candidate is comparing the stack-top Z between consecutive source probes.
- Speed work comes from fewer sleeps, no staging, and computing while the machine moves. It never comes from overlapping axes.

## Reversibility (owner requirement, 2026-09-27)

The staging-platform cycle must stay fully working until the up-camera cycle has proven itself on real cards. Switching back must be a settings change plus swapping the hardware mount back, with no code changes.

- **One switch.** The `sort_cycle` setting (`staging` | `upcam`, default `staging`) picks the per-card cycle. The new cycle lives in its own function or module next to the existing one. The staging path is not edited, except for bug fixes that apply to both.
- **Camera role.** `id_role` in `camera_config.json` (`down` | `up`) picks the identification camera. `sort_cycle: upcam` requires `id_role: up`, and the session preflight enforces this.
- **Keep the staging data.** Setup schema v2 keeps the `staging` block alongside the new `upcam` block. Staging calibration files are not deleted.
- **Shared code is additive.** Motion primitives, the detector and tests for the new cycle are new code. Existing primitives are not changed to suit the new cycle.
- **Deletion comes last.** TASK-103 (delete the staging artifacts) only happens after the owner signs off on the up-camera cycle.
- **Git as a backstop.** Each step goes on its own branch or PR, so any step can be reverted on its own.
