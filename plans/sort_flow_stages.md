# Sort Flow — Staged UX

The Sort tab is the user's main workspace and walks them through three
stages. Each stage exposes only what's relevant *now*; everything else
is hidden. This is progressive disclosure on the live workflow — not
hiding features behind menus, but hiding them behind *time*.

This doc is the design spec for the Sort tab rebuild that lands in
Phase 4 (polish) once visual identity is settled. It supersedes the
current "everything stacked vertically" Sort tab from Phase 1.

## The three stages

```
   ┌──────────────┐   Start   ┌──────────────┐   end / Stop  ┌──────────────┐
   │  PRE-SORT    │  ───────> │   RUNNING    │  ───────────> │  POST-SORT   │
   │  (configure) │           │   (live)     │               │  (review)    │
   └──────────────┘           └──────────────┘               └──────────────┘
          ^                                                          │
          │                  Start another session                   │
          └──────────────────────────────────────────────────────────┘
```

The Sort tab is always in *exactly one* stage. The navbar (E-stop,
connection state, gear, badges) is constant across all three. The
tab's own header changes — see "Header per stage" below.

---

## Stage 1 — Pre-sort

**Goal:** the user picks what cards go in what bins, picks where the
cards are coming from, and presses Start.

### Visible

- **Source picker.** Two options:
    - *Input tray* (default — the physical source bin loaded with cards)
    - *Re-sort an existing box* (dropdown of the user's Boxes; selecting
      one means the machine pulls from this box instead of the input tray
      for this session)
- **Sort config editor.** The bin-by-bin query table:
    - Per-row: bin number · query string · override flag · overflow target
    - Toolbar: load preset · save · save as · duplicate · validate queries
    - Add/remove bin rows (within the configured bin count)
- **Active preset name** with dirty indicator if edits aren't saved.
- **Start** button (primary, large, only enabled when queries validate).
- **Calibration status pill.** Quiet by default ("Calibrated"). Only
  prominent if calibration is stale or incomplete — clicking it deeplinks
  to the Setup tab's calibration wizard.

### Hidden

- Everything from Running and Post-sort stages.
- Detailed bin position editing (calibration concern → Setup).
- Test scan (diagnostic — lives on the Setup tab).

### User actions

- Edit queries inline; validation runs as they type.
- Load a saved preset; presets persist independently from the active
  config (the user can edit a loaded preset without overwriting it).
- Save / save as / duplicate to manage their preset library.
- Pick a source — defaults to *input tray*.
- Press Start.

### Transition: Start

When Start is pressed:

1. Worker validates queries one more time (prevents typos slipping
   through if the user pasted from somewhere).
2. Worker confirms hardware is connected and homed; if not, prompt
   "Home all axes now?" (per autonomy ladder L3).
3. If source is a Box, worker stages the re-sort logistics (open
   question — multi-pass sorting plan covers this).
4. UI transitions to Running stage. The pre-sort editor is hidden
   from view; the user's choices are now the running session's
   configuration.

---

## Stage 2 — Running

**Goal:** the user sees what's happening, intervenes if they want, and
otherwise lets the machine work. The screen should feel calm — fast
flicker would be exhausting on a long run.

### Visible

- **Live camera view** of the staging platform. The current card under
  identification is the visual focus.
- **Session stats strip.** Three numbers:
    - Cards sorted (running count)
    - Elapsed time
    - Throughput (cards/min, rolling average)
- **Source bin status.** Estimated remaining count from probe height vs.
  empty-bin reference, shown as a number plus a small horizontal bar.
  Updates whenever the source bin is probed.
- **Bin tile row.** One tile per destination bin, in bin-number order:
    - Bin number + role badge (override / wishlist / priority / fallback)
    - Query string (truncated with full text on hover/tap)
    - Current count
    - Last card dropped (small thumbnail + name)
    - Per-bin **Empty** button (small, tucked in the corner)
    - Fullness indicator (subtle bar; turns warning when near capacity)
    - Hovering / tapping the tile reveals a scrollable strip of recent
      cards dropped here (image previews, same pattern we use today)
- **Pause** and **Stop** primary controls (visually distinct from
  E-stop, which is the navbar's nuclear button).
- **Activity log** (collapsible side panel): rolling list of events
  (card dropped, retry, no-detect, wishlist hit, etc.).
- **Wishlist toast** (when applicable): appears on the first wishlist
  hit, lists the cards found, accumulates additional finds inline
  rather than stacking new toasts. Per the autonomy ladder spec.

### Hidden

- Everything from Pre-sort (sort config editor, source picker, etc.).
- Everything from Post-sort.
- Calibration controls.

### User actions during Running

- **Pause.** Worker finishes the current card cycle, then idles. The
  session state persists; resume picks up exactly where it left off.
  Pause is undo-friendly (no destructive action).
- **Resume** (replaces Pause when paused). Continues the session.
- **Stop.** Worker finishes the current card cycle, then ends the
  session cleanly. Transitions to Post-sort stage. Stop is *not*
  destructive — it commits the work done so far to the Collection.
- **Empty a bin.** User clicks the per-bin Empty button. Worker
  confirms ("Bin 3 emptied? It currently shows 287 cards.") and
  offers an optional **Where did these cards go?** prompt with three
  choices:
    - *Skip* — just clear the bin, no tracking
    - *Add to box* — pick an existing Box (or create one); cards
      are reassigned to that Box in the Collection
    - *Add to box + divider* — pick a Box and either pick an
      existing divider or create a new one with a label/code (e.g.
      "R1", "Removal", "Mythics"); cards are reassigned to that
      Box behind that divider
  None of this is required — Skip is the default and one-tap.
  Continuous sort doesn't pause for the empty action; the user can
  do this while the machine is picking from another bin.
- **Hover/tap a bin tile.** Reveals recent cards in that bin
  (image previews, name, confidence). Doesn't pause the sort.

### Auto-handling per autonomy ladder

- **Low-confidence card identification:** card goes to fallback bin,
  added to detection review queue silently. No interruption.
- **Low-confidence on foil/border/frame/set:** card goes to its matched
  bin, added to detection review queue silently. No interruption.
- **Destination bin full + overflow set + overflow not full:** route
  silently. Activity log entry only.
- **Destination bin full + no overflow OR overflow also full:**
  pause the session and prompt "Bin 3 is full — empty it then resume?"
  with a Resume button (one tap). E-stop still available.
- **Source bin empty:** stop session, transition to Post-sort with end
  reason "out of cards." Activity log notes how many were sorted.
- **Three consecutive no-detects:** stop session, transition to
  Post-sort with end reason "consecutive no-detects." Hint user that
  the source bin may be jammed or the lighting / staging is off.
- **Mechanical fault:** stop session, transition to Post-sort with
  the fault reason in the summary. E-stop button on the navbar
  (and physical button on the machine) always available.

### Transition: Stop / session ends

Worker emits a `session_ended` event with the end reason. UI
transitions to Post-sort. The bin tile row (with final counts) and the
last camera frame remain visible during the transition so the user
isn't jarred to a different layout.

---

## Stage 3 — Post-sort

**Goal:** the user sees what just happened, fixes anything that needs
fixing, and decides what to do next.

### Visible

- **Session summary card.** End reason, total cards sorted, total
  time, throughput, total no-detects, total retries. If any wishlist
  cards were found, list them inline with thumbnails.
- **Per-bin final counts.** Same bin tile row as Running stage but
  static — no last-card thumbnail flicker, just the final count, the
  query, and the Empty button (still useful — user wants to clear bins
  before the next session).
- **Detection review queue (this session).** A scrollable list of
  scans flagged as low-confidence during this session. Each row:
  thumbnail, machine guess, confidence label, dropped bin, "Confirm"
  / "Correct" / "Hash diagnostics" buttons. The queue is a *post-hoc*
  review surface — the cards have already been routed. Empty state:
  "All cards identified confidently this session — nothing to review."
- **Primary actions:**
    - **Start another session** — returns to Pre-sort with the same
      config preselected. One tap to continue working.
    - **Edit config** — returns to Pre-sort with the same config but
      no auto-Start, so the user can tweak.
    - **Export this session to CSV** — saves the per-card scan list.
- **History link** (small, unobtrusive — e.g. "View past sessions"
  link in the Post-sort header). Opens a modal listing recent
  sessions with timestamps, end reasons, totals. Click any row to
  expand its summary. Doesn't compete with the primary actions for
  visual real estate — past-session lookups are infrequent.

### Hidden

- Pre-sort editor (until "Start another" or "Edit config" pressed).
- Live camera feed (the session is over; turning the feed off saves
  bandwidth on a Pi).
- Active stats / source-bin gauge.

### User actions

- **Confirm or correct each review-queue item.** The same
  per-attribute review interactions we have today, just lives here
  now instead of on Setup.
- **Apply correction to similar matches** (per autonomy ladder L2 with
  user double-check): if the user's correction looks like it could fix
  multiple pending matches, the queue offers a batch suggestion which
  the user previews item-by-item.
- **Start another / Edit / Export** as listed above.
- **Empty bins** before the next run (same per-bin button as Running).

### Transition: Start another / Edit config

Returns to Pre-sort. The previous session's config is loaded as the
active config; the user can tweak or just press Start again.

---

## Cross-stage behaviors

### Header per stage

The Sort tab's own header (below the global navbar) changes per stage:

- Pre-sort: title "Configure Sort" + active preset name + dirty pill
- Running: title "Sorting…" + session timer + Pause/Stop buttons
- Post-sort: title "Session complete" + end reason + Start-another button

The tab's nav-link label stays "Sort" in all three stages — it's the
*tab*, not the stage.

### Power-loss resume

On startup after an unclean shutdown (per the autonomy ladder L3
power-loss-resume entry):

1. Detect last session was active and didn't end cleanly.
2. UI loads directly into the Sort tab in a special "Resume?" state
   — kind of a between-stages limbo. Shows the last session summary
   ("47 cards sorted, ended unexpectedly") and two buttons: **Resume
   session** (returns to Running) or **Discard and start fresh**
   (returns to Pre-sort).
3. If neither is pressed within N seconds, no auto-action — the
   machine waits for the user.

### E-stop behavior across stages

E-stop is the navbar's red button + the physical button on the
hardware. Pressing either:

- Pre-sort: harmless (nothing's running). Worker re-homes.
- Running: motion halts immediately, current card may be in flight.
  Session pauses (not stopped). UI shows an E-stop banner; "Reset &
  Re-home" button clears Marlin halt and returns to a Resumable state.
- Post-sort: same as Pre-sort — harmless.

There's an existing open bug
([project_estop_reset_rehome_doesnt_resume.md](C:\Users\Jon\.claude\projects\D--Card-Sorter-Scripts\memory\project_estop_reset_rehome_doesnt_resume.md))
where Reset & Re-home doesn't actually resume cleanly. That fix is a
prerequisite for this flow to work correctly. Worth flagging in the
Phase 4 implementation plan.

### Navigating away during a session

If the user clicks Collection or Setup during Running stage:

- The worker keeps running — the sort doesn't pause just because the
  user is on a different tab.
- The navbar shows a small "session in progress" indicator so they
  can find their way back.
- Returning to Sort puts them back in the Running stage view.

### App restart during a session

Different from power-loss: app process exits (e.g., Pi reboot) but
the machine wasn't physically powered off. Treat the same as
power-loss-resume — the persisted session state is the source of
truth.

---

## What lives in Setup, not Sort

Belongs explicitly to the Setup tab so the Sort tab stays focused:

- **Calibration wizard** (ArUco + drop tuner + camera offset).
- **Hardware connect / home / disconnect controls** (the auto-connect
  on boot covers the common case).
- **Camera start / stop** (the Sort tab's running stage starts the
  feed automatically when entering, stops on exit).
- **Test scan** — diagnostic for verifying detection accuracy without
  doing a real sort. Lives on Setup.
- **Detection review** of *historical* sessions (not the current one).
  The Sort post-sort stage shows the current session's review queue;
  historical reviews live on Setup as a diagnostic.

## Resolved decisions

1. **Test scan** lives on Setup (diagnostic, not part of running a sort).
2. **Past sessions** — small unobtrusive history link in the Post-sort
   header opens a modal. Doesn't compete with primary actions.
3. **Re-sort source flow.** When the user picks an existing Box as
   source, the assumption is they'll physically transfer the box's
   cards into the input tray themselves; the Box-as-source choice is
   just metadata for the resulting ScanSession (so we can record that
   the cards came from / are returning to a particular Box). No
   step-by-step transfer wizard.
4. **Pause is a primary button** in the Running stage. The user uses
   it to empty bins and refill the source while the machine is paused
   — common enough that it earns the visual weight.
5. **Activity log** is a collapsible side panel.
6. **Wishlist toast** stays until dismissed. The toast is the running
   list of wishlist cards found this session; new hits append; user
   dismisses when they're ready. No auto-dismiss timer.

## Multi-pass / re-sort related

The Box → re-sort flow assumes the user physically empties the box
into the input tray before pressing Start. Open logistical question
([project_multi_pass_sorting.md](C:\Users\Jon\.claude\projects\D--Card-Sorter-Scripts\memory\project_multi_pass_sorting.md))
remains: is multi-pass actually faster than re-sorting from scratch?
That's a workflow / time-budget question, not a UX-flow question.

## Implementation order (Phase 4)

When this gets built (after visual identity returns from Claude
design):

1. Skeleton: three stage components in [_tab_sort.html](../templates/partials/_tab_sort.html)
   conditionally rendered based on a `currentStage` JS state variable
   (`'pre' | 'running' | 'post'`).
2. Pre-sort stage first (mostly already exists — re-arrange).
3. Running stage's bin tile row (new component, replaces the dense
   bin-contents panel).
4. Source bin estimated count from probe height (new feature; backend
   needs to surface probe-vs-empty-reference comparison).
5. Post-sort stage (new — review queue moves from Setup to here).
6. Power-loss resume "limbo" state.
7. Wire stage transitions; remove deprecated controls.

Each step is an independent PR that can be reviewed and reverted. The
existing flat layout from Phase 1 keeps working until the rebuild
is fully merged.
