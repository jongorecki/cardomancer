# Autonomy Ladder — Card Sorter

A reusable design rubric for deciding *how much* the system should do
on its own for each automation feature. Adapted from Eleken's
"Invisible UX" framework; tailored for our hardware + CV product.

The right level for any feature depends on three factors:

1. **Reversibility** — can the user easily undo a wrong action?
2. **Cost of a wrong action** — wasted time, lost data, damaged
   hardware, lost user trust.
3. **Trust earned** — has the system proved itself on this user's
   data at this confidence level?

Higher autonomy is appropriate when reversibility is high and cost
is low. Drop one level whenever a feature is touching new territory
(unfamiliar lighting, new card sets, new hardware variant).

## The five levels

| L | Name | Pattern | Example domain |
|---|---|---|---|
| 1 | **Manual** | User does every step. System exposes controls. | Drop-height tuner step buttons |
| 2 | **Suggested** | System proposes; user accepts or rejects before any action. | ArUco-detected bin positions, "Use these?" |
| 3 | **Auto-with-confirm** | System computes the action; user confirms once before it executes. | Hash DB swap after Scryfall update download |
| 4 | **Auto-with-undo** | System acts immediately; user can undo after the fact. | Continuous sort + "Undo last sort" |
| 5 | **Fully autonomous** | System acts, no user touch, no undo surface. Used only for self-recovery and internal subroutines. | Z-bounce retry on stuck pickup |

## Mapping of current and planned features

### Sorting (the core loop)

**Guiding rule:** the machine should never stop on its own unless it has
to. "Has to" means: source bin empty, destination bin full with no
overflow available, or a mechanical fault. Low confidence on a card's
identity is **not** a stop condition — it routes to the fallback bin
and lands in the detection review queue.

| Feature | Today | Should be | Notes |
|---|---|---|---|
| Continuous sort (pick → identify → drop) | L4 | **L4** | Correct. "Undo last sort" is the safety net. |
| CV identification — *what card it is* | L5 | **L4 always; review-queue tag if low confidence** | Always sort the card; never pause for confirmation. High confidence → drop into the matched bin. Low confidence → drop into the **fallback bin** AND add to the detection review queue. The threshold for "low confidence" should be tuned high — most cards are identified correctly today, and the review queue should only catch genuinely uncertain ones. |
| CV identification — *foil / border / frame / set* | L5 | **L4 always; review-queue tag silently** | These attributes aren't critical for sort routing. If uncertain, sort the card normally to its bin and add it to the review queue for later double-check. Never delays the sort. |
| Bin assignment from sort rule | L4 | **L4** | Correct. |
| Pickup retry (Z-bounce on stuck cards) | L5 | **L5** | Internal recovery, fast, fully reversible (the card stays in the bin if it doesn't lift). Keep autonomous. |
| No-detect retry | L5 | **L5 with hard cap** | Already capped at 3; correct. |
| Destination bin full | n/a | **L4 if overflow available; L3 prompt if not** | If the bin's overflow bin is set and not also full, route there silently. If no overflow is set OR the overflow itself is full, pause and prompt the user: "Bin 3 is full — empty it, then resume?" Resume is one tap. |
| Overflow chaining | L4 | **L4** | Correct. Chains can be multi-step (Bin 3 → Bin 7 → Bin 10). |
| Source bin empty | L5 (stop) | **L5 (stop)** | Correct — there's nothing to do. UI should clearly say "out of cards" so user knows to refill. |

### Calibration / setup

| Feature | Today | Should be | Notes |
|---|---|---|---|
| Web server auto-connects to hardware on boot | n/a (manual) | **L5 (silent)** | Required for the L3 home-on-connect prompt to work. On boot, the server attempts the connection in the background; the UI shows the connection state in the navbar. Failure is non-fatal — user can manually retry from Setup. |
| Hardware homing on connect | L1 | **L3** | Auto-home with confirm — "Home all axes now?" once on first successful auto-connect after power-up. Keep manual buttons available. |
| ArUco bin-position detection | L1 | **L2** | Detect markers, show proposed positions overlaid on the camera feed, "Apply these?" Correct already in spirit; tighten the UI. |
| Drop-height tuner | L1 | **L1 + suggested defaults** | Manual stepping is correct (it's a physical calibration). But pre-populate a sensible default per card thickness. |
| Camera offset | L1 | **L2** | After ArUco run, propose a camera offset based on observed marker positions. |

### Data & enrichment

| Feature | Today | Should be | Notes |
|---|---|---|---|
| Hash DB rebuild on new Scryfall release | n/a (manual) | **L3** | Auto-download bundle on startup, show "New card data available — install?" with timestamp + version. User confirms; system swaps atomically with rollback. |
| Embedding DB rebuild | n/a | **L3** | Same as hash DB. Bundled together. |
| EDHREC staples refresh | L4 (scheduler) | **L4** | Background refresh on its schedule; user sees a status badge. Correct. |
| Buylist price refresh | L4 | **L4** | Same. |

### Detection review queue

The queue exists to catch *post-hoc* uncertainty, not to stop the sort.
Cards in the queue have already been routed to a bin (their matched
bin if foil/border/set was uncertain, or the fallback bin if the
card identity itself was uncertain).

| Feature | Today | Should be | Notes |
|---|---|---|---|
| Low-confidence on card identity | L2 (after the fact) | **L2 (after the fact)** | Card already went to fallback bin; user reviews queue at their leisure to correct or confirm. Never blocks an active sort. |
| Low-confidence on foil / border / frame / set | L2 (after the fact) | **L2 (after the fact)** | Card already went to its matched bin; user reviews to correct attribute metadata. Doesn't change bin routing. |
| User correction → re-train hint | L1 | **L2 with user double-check** | When user submits a correction, surface "Apply this correction to similar pending matches?" Suggested batch mode — but each affected match still requires the user to glance at it before accepting, in case the situation is unique. |

### Wishlist / priority routing

| Feature | Today | Should be | Notes |
|---|---|---|---|
| Wishlist hit detection | L4 | **L4** | Auto-route to wishlist bin; undo via re-sort. |
| Priority routing (e.g. "$1+ override") | L4 | **L4** | Same. |
| Wishlist hit alert | L5 (badge) | **L3 toast, accumulates** | First wishlist hit per session: show a toast that lists the card found and the bin it went to ("Found *Sol Ring* — sent to bin 7"). Subsequent hits in the same session: append to the existing toast (don't show a new one). The toast becomes a running list of wishlist cards found in this session, dismissible by the user. The badge in the navbar still updates with the running count. |

### Session lifecycle

| Feature | Today | Should be | Notes |
|---|---|---|---|
| Power-loss resume | n/a (planned) | **L3** | On startup after unclean shutdown: "Resume previous session (47 cards sorted)?" with Resume / Discard. |
| Session auto-stop on N consecutive errors | L5 | **L5** | Correct — safety mechanism. |

## Principles

1. **Default to L4 for the user-facing critical loop**, with L2 escape
   hatches when confidence is low. The user wants the machine to *do
   the work*; the value proposition collapses if they have to confirm
   every card.

2. **Reserve L5 for internal recovery routines only.** Anything the
   user could plausibly want to know happened belongs at L4 with a
   visible "what just happened" trace (the activity log), not L5.

3. **Anything that swaps shipped data (hash DB, embedding DB,
   firmware) is L3, never L4.** The cost of a silent bad swap is
   high (sorting accuracy drops, user blames the machine). One
   confirmation is cheap insurance.

4. **Drop one level on a fresh / uncalibrated hardware setup.** First
   run on newly assembled hardware, before drop-tuner / ArUco /
   camera-offset have been run: bias toward L2/L3 confirms until the
   user has run the calibration wizard end-to-end. Once calibrated,
   the system runs at full L4 autonomy.

   **Note on what this rule does NOT cover.** Lighting is provided by
   the machine and is consistent in normal operation, so new card
   sets and ambient-lighting variation are *not* unfamiliar
   territory — as long as the hash/embedding DBs cover the new set,
   identification works. Different hardware variants are also out of
   scope; if a future variant ships, it will be a totally retuned
   system and gets its own calibration baseline rather than a
   confidence-threshold dial.

5. **Always log autonomous actions.** L4 and L5 actions must be
   visible after the fact in the activity log. Without a trace, users
   can't tell if the machine is broken or just being silent.

## Bin capacity baseline

Working assumption: ~300 cards per destination bin before the Z-probe
fullness check trips. Use this as the default when a sort config
doesn't specify a `limit:`. Defined as `config.DEFAULT_BIN_CAPACITY`.

## When to revisit

Re-read this doc whenever:
- Adding a new automation feature (decide its level *first*)
- A user says "I don't trust it" or "why did it do that?" (it's
  probably one level too high)
- A user says "stop asking me" (it's probably one level too low)
- The CV stack changes (new model, new threshold) — re-validate the
  L4/L2 split for identification.
