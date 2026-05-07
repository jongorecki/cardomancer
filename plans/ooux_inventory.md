# OOUX Inventory — Card Sorter

Object-Oriented UX inventory of the user-facing concepts in the Card
Sorter. The point: validate that our IA (Sort / Collection / Setup
and Settings) matches users' mental models *before* visual polish, by
making the underlying objects explicit.

For each object: **attributes** (what data it has), **actions** (what
the user can do to/with it), **relationships** (how it connects to
other objects), and **primary surface** (which tab/screen is the
canonical home).

To validate this list, run a card-sort test with 3-5 actual TCG
players. Print each object name on a card, ask them to group the
cards into "things that go together," and compare their groupings
to our tab structure. Disagreements between users — or between
users and our tabs — are the highest-value findings.

---

## The objects

### Card
A single Magic card the user owns, scanned once.

- **Attributes:** name, set code, collector number, oracle ID,
  rarity, color identity, mana value, type line, price (USD,
  buylist), is foil, is cull-candidate, is wishlist-match,
  printing-disambiguation outcome, scan timestamp, scan confidence,
  hash distance, image crop URL, **current box assignment, current
  box-divider assignment**, current bin assignment.
- **Actions:** view detail, view on Scryfall, mark as foil
  (override), correct identification, view all printings, increment
  quantity, decrement quantity, delete, **assign to box, assign to
  divider within a box**, mark as wishlist, mark as cull.
- **Relationships:** belongs to a Collection, was created in a
  Scan within a ScanSession, lives in a Box, optionally lives behind
  a BoxDivider within that Box, optionally matches a Wishlist entry,
  has many printings (variants).
- **Primary surface:** Collection (browse), Sort (live readout
  during scan).

### Bin
A tray on the machine. **Destination bins** receive sorted cards;
**source bins** hold the input stack to be sorted.

- **Attributes:** bin number, role (source / destination), X
  position, full/empty state, current count (destination: counted
  card count; source: estimated count from probe height vs. empty
  reference), capacity, query (destination only — what cards go
  here), is override, is fallback, is wishlist bin, is priority
  bin, overflow chain (destination only).
- **Actions:** assign query (destination), mark empty
  (destination), edit position (manual or auto via ArUco), set as
  wishlist/priority/fallback, configure overflow chain, view
  estimated source count.
- **Relationships:** receives Cards during a ScanSession
  (destination), supplies Cards to a ScanSession (source), is
  configured by a SortConfig (overflow + queries), has positions
  defined in a CalibrationProfile.
- **Primary surface:** Sort. Bin queries + overflow (sort config)
  are edited in the pre-sort stage; live state and per-bin Empty
  buttons are visible in the running stage; source-bin estimated
  count is visible at the top of the running stage so the user
  knows roughly how much is left to do.

### ScanSession
One run of continuous sort — start, sort N cards, end. The source
of cards may be the physical input tray (the normal case) or an
existing Box being **re-sorted** into a new arrangement (the
multi-pass case — hardware supports this; logistics still TBD).

- **Attributes:** start time, end time, duration, total cards
  scanned, total cards sorted, total no-detects, total retries,
  sort config used, calibration profile used, source (input tray
  or Box ID), list of Scan IDs in this session, end reason
  (user-stopped / bin-full-no-overflow / source-empty /
  consecutive-no-detects / e-stop / mechanical-fault).
- **Actions:** start, pause, resume, stop, view summary, export to
  CSV, delete, view session history, re-sort an existing Box.
- **Relationships:** uses one SortConfig, uses one
  CalibrationProfile, has one source (input tray or Box), contains
  many Scans, produces Card additions or reassignments to the
  Collection.
- **Primary surface:** Sort (live and history).

### Scan
One card was picked, identified (or not), dropped.

- **Attributes:** scan ID, timestamp, identified card (or null),
  confidence bucket (high / review / low), bin number dropped to,
  pickup retry count, hash distance, embedding distance, image
  crop, manual correction (if any).
- **Actions:** view detail, correct identification, view hash
  diagnostics, undo (if last scan), view image crop.
- **Relationships:** child of one ScanSession, identifies (or
  fails to identify) one Card, may have a user correction.
- **Primary surface:** Sort. Live during the session; review queue
  appears in the post-sort stage of the same Sort tab so the user
  can verify any low-confidence cards immediately after a run
  without changing tabs.

### SortConfig (Preset)
A named mapping of bin numbers to queries.

- **Attributes:** name, bin count, per-bin query, override flags,
  fallback bin number, wishlist bin number, priority bin number,
  is dirty (unsaved edits), file path.
- **Actions:** save, save as, load, delete, duplicate, edit row,
  add bin, remove bin, validate query, open query on Scryfall.
- **Relationships:** used by ScanSessions, references query syntax
  (incl. `staple:`, `otag:`, `buylist:` predicates fed by
  EnrichmentSources).
- **Primary surface:** Sort.

### Box
A physical storage container where Cards live after sorting. Boxes
can be used as the **source** of a future ScanSession when the user
wants to re-sort an existing box into a new arrangement (e.g., a
"red creatures" box re-sorted by mana value into dividers).

- **Attributes:** box ID, name, location notes, current card
  count, capacity, divider count, last-modified.
- **Actions:** create, rename, delete, view contents, bulk-assign
  cards to/from, locate cards in box, manage dividers (add /
  remove / reorder / rename), **re-sort this box** (start a new
  ScanSession using this box as source instead of the input tray).
- **Relationships:** contains many Cards, contains many
  BoxDividers, may serve as the source for a ScanSession,
  referenced by Locator queries.
- **Primary surface:** Collection. The "re-sort this box" action
  hands the user off to the Sort tab's pre-sort stage with the box
  pre-selected as the source.

### BoxDivider
A physical divider inside a Box that splits the box into named
sections so the user can find specific cards faster. The schema
already exists in `collection_db` (`dividers` table).

- **Attributes:** divider ID, parent box ID, label (e.g. "Removal",
  "Red creatures"), position within box (ordering), capacity (cards
  this section can hold), created-at.
- **Actions:** add to box, rename, reorder, remove, view contents,
  assign cards to this divider.
- **Relationships:** belongs to one Box, contains many Cards.
- **Primary surface:** Collection (managed inline as part of the
  Box detail view).

### Wishlist
Cards the user wants to acquire.

- **Attributes:** card name, source (Moxfield deck / manual entry
  / EDHREC list), priority, quantity wanted, found-flag.
- **Actions:** add manually, import from Moxfield, delete, mark
  found, view source list.
- **Relationships:** matches Cards by oracle_id during ScanSessions,
  can be sourced from a Moxfield import.
- **Primary surface:** Sort (configuring routing) and Collection
  (browsing the list).

### Collection
The user's entire scanned inventory across all sessions.

- **Attributes:** total cards, unique cards, total value (USD),
  total value (buylist), per-set / per-color / per-type breakdowns.
- **Actions:** search, filter, sort columns, paginate, export to
  CSV, import from CSV, reset, view stats.
- **Relationships:** aggregates all Cards across all ScanSessions.
- **Primary surface:** Collection.

### CalibrationProfile
Hardware-specific setup parameters.

- **Attributes:** name, bin X positions, source bin X position,
  staging X, camera X offset, Z drop height, ArUco marker
  references, last-applied timestamp.
- **Actions:** save, save as, load, delete, run ArUco detection,
  run drop-height tuner, manually override positions.
- **Relationships:** used by ScanSessions, hardware-specific (a
  profile from one machine doesn't transfer to another).
- **Primary surface:** Setup.

### EnrichmentSource
External data feeders (Scryfall, EDHREC, edhtop16, Tagger, buylists).

- **Attributes:** name, last-refresh time, status (ok / error /
  refreshing), row count, schedule, error message.
- **Actions:** refresh now, view status, configure schedule.
- **Relationships:** feeds query predicates used in SortConfig
  (e.g. `staple:any`, `buylist:ck`, `otag:removal`).
- **Primary surface:** Settings (gear modal).

---

## Primary-surface summary (validates the IA)

| Object | Sort | Collection | Setup | Settings |
|---|---|---|---|---|
| Card | live readout | browse, edit | — | — |
| Bin | configure + live + per-bin Empty | — | (positions only via ArUco wizard) | — |
| ScanSession | live + history + re-sort entry | — | — | — |
| Scan | live + review queue | — | — | — |
| SortConfig | edit + use | — | — | — |
| Box | re-sort entry | manage | — | — |
| BoxDivider | — | manage (inside Box) | — | — |
| Wishlist | configure routing | browse list | — | — |
| Collection | — | the whole tab | — | — |
| CalibrationProfile | — | — | manage | — |
| EnrichmentSource | — | — | — | manage + status |

The IA looks coherent: every primary surface has a clean object
focus, no object is split awkwardly across two homes, no surface is
overloaded with unrelated objects. Two notes:

1. **Bin position is the only calibration-flavored attribute on
   Bin.** Queries and overflow are sort configuration, not
   calibration — they live in Sort. Bin physical positions (X
   coordinates) are calibration and live in Setup as part of the
   ArUco / hardware setup wizard. No conflict: the same Bin object
   shows up on both surfaces but with different attributes
   exposed.
2. **Wishlist** straddles Sort (routing config) and Collection
   (browsing the list). That's natural for the object — it is both
   a routing input and a browsable list. No fix needed; just be
   careful when editing wishlist UI to keep both surfaces in sync.

## Sort tab is task-focused, not feature-listed

A core design principle that emerged while writing this inventory:
the Sort tab is not a single dense screen with every sort-related
control visible at once. It walks the user through the natural
stages of a sort and only exposes controls relevant to the current
stage:

1. **Pre-sort stage** — bin queries + overflow + source selection
   (input tray or a Box). Big "Start" button. Calibration status
   shown if anything is stale; otherwise quiet.
2. **Running stage** — camera view of the staging area, sort
   session stats (cards sorted, time, throughput), a row of bin
   tiles each showing its query + count + last card dropped + a
   per-bin Empty button. Source bin shows estimated card count
   from probe height vs. empty reference. Stop and Pause are the
   primary controls. Hover/tap on any tile reveals the cards
   recently dropped (image preview).
3. **Post-sort stage** — session summary, the detection review
   queue for any low-confidence scans from this session, and an
   "export" / "start another session" path. Past session history
   lives here too.

Implication for the IA: every object in the table above is
present on Sort, but at different stages. The "Bin" cell in the
table reads "configure + live + per-bin Empty" because the user
configures during pre-sort and runs during running stage. This is
exactly the progressive-disclosure pattern Eleken and the Senja
case study recommend.

**A separate doc** ([plans/sort_flow_stages.md](sort_flow_stages.md))
will describe the stages in detail — what's visible, what's
hidden, what transitions between them, and how Stop/Pause/Resume
behave. Listed here because the IA only makes sense in light of
the flow.

## Validation (deferred)

A user-card-sort test (3-5 TCG players, paper cards, group
into "things that go together") would empirically validate the
groupings above. Not planned for now — the IA is task-focused and
matches the user's mental flow. Park this as an option to revisit
if usability feedback after launch suggests the IA is fighting
people's models.
