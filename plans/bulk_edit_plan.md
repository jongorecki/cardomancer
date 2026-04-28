# Bulk Edit + Clipboard Plan

## STATUS

Drafted 2026-04-27. Not started.

Phase A is the v1 deliverable. Phase B / C / D are follow-ups, listed here so the v1 schema doesn't paint us into a corner.

---

## Goal

Treat card lists as plaintext interchange artifacts. Every list in the
app — collection, box, binder, deck, wishlist, Moxfield-pull, hand-picked
selection — has a textual representation. The clipboard is the universal
pipe: text leaves the app via copy, enters via paste.

The bulk-edit tab is the **input** workhorse — paste-target with
diff-preview-then-apply. Selections-to-clipboard handle the **output**
side. Together they replace every "import / export / move" UI we'd
otherwise have to build per-list-type.

## Non-goals (v1)

- No Moxfield write-back via API (read-only public endpoints stay; user
  copy-pastes our text into Moxfield's own Bulk Edit).
- No OAuth / Moxfield private decks.
- No saved-filter "lists" (Phase D).
- No box/divider table (Phase C).
- No conflict resolution / merge strategy.

---

## Provenance — the schema work that has to land first

Today every `inventory` row is treated identically regardless of how it
got there. After bulk edit ships, that's broken: a card the camera saw
is qualitatively different from a card the user typed in.

### Schema

```sql
ALTER TABLE inventory ADD COLUMN provenance TEXT NOT NULL DEFAULT 'scan';
-- Values: 'scan' | 'manual' | 'moxfield_pull' | 'mixed' | 'gone'
--   scan          — backed by ≥1 scan_history row (the default for any existing row)
--   manual        — added by paste-import; no scan
--   moxfield_pull — pulled from a Moxfield deck/wishlist; no scan
--   mixed         — was 'manual' or 'moxfield_pull', then later scanned. Promoted automatically.
--   gone          — was scanned, then removed by a bulk edit. Quantity goes to 0 but the row
--                   stays so scan_history doesn't get orphaned.

CREATE TABLE inventory_history (
    id            INTEGER PRIMARY KEY,
    inventory_id  INTEGER NOT NULL REFERENCES inventory(id),
    timestamp     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    operation     TEXT NOT NULL,
        -- 'manual_add' | 'moxfield_add' | 'move' | 'remove' | 'reconcile' | 'qty_change'
    box_or_loc    TEXT,                  -- destination after the operation
    qty_delta     INTEGER,               -- signed quantity change
    note          TEXT,                  -- optional human-readable context
    op_id         TEXT                   -- groups all rows from one bulk-edit apply
);
CREATE INDEX idx_inventory_history_inventory_id ON inventory_history(inventory_id);
CREATE INDEX idx_inventory_history_op_id ON inventory_history(op_id);
```

### Migration

`collection_db._create_tables` adds the column + table on first connect
to existing DBs (same pattern as the recent `frame/border_color/
frame_effects` migration). Existing inventory rows backfill to
`provenance = 'scan'` since they all came from sorting.

### Storage cost at 40k-card scale

inventory: +1 small column → ~5 MB. inventory_history: ~80k rows over
a year of casual moves → ~10 MB. Negligible.

### Promotion to 'mixed'

When `record_scan` writes to a `(set, cn)` whose existing inventory row
has `provenance != 'scan'`, the row's provenance gets bumped to
`'mixed'` and an `inventory_history` row with `operation='reconcile'`
is appended. No data loss.

---

## Plaintext interchange — extend `moxfield_text.py`

`web_enrichment/moxfield_text.py` already has `parse_text` /
`resolve_entries` / `render_text` for the Moxfield format. Two
extensions for v1:

### 1. Auto-detect parser (input)

`parse_text(text, format='auto')` sniffs the first 5–10 non-comment
lines and picks a strategy. Detection signals:

| Format | Tell |
|---|---|
| Moxfield | `4 Lightning Bolt (LEA) 161` qty-name-paren-cn |
| MTGA / Arena export | section markers `Deck` / `Sideboard` / `Companion` |
| MTGGoldfish | name TAB qty |
| TCGplayer mass entry | `1x` prefix, no parens |
| Archidekt | qty + name + (set) lower |
| Plain names | one name per line, no leading digit |
| Plain qty-name | `4 Lightning Bolt`, no parens, no `x` |

Lines that don't parse become `// UNRECOGNIZED: <line>` markers in the
returned entry list rather than silent drops. The diff preview surfaces
these so the user can fix typos.

### 2. Multi-format renderer (output)

`render_text(entries, format)` where format ∈
`{moxfield, plain, mtggoldfish, archidekt, csv, json, names_only}`. All
small variations on the existing `render_text` — most are 5-line
renderer functions.

---

## API contract

### `POST /api/bulk-edit/apply`

Single-source, single-target, single-operation. Atomic per `op_id`.

```jsonc
{
  "source_kind":  "inventory" | "wishlist" | "moxfield_deck" | "moxfield_wishlist" | "text",
  "source_key":   "box:Box A" | "deck:abc123" | "username" | null,    // null when source_kind='text'
  "source_text":  "...",        // present iff source_kind='text'
  "target_kind":  "inventory" | "wishlist" | "moxfield_wishlist",
  "target_key":   "box:Box B" | null,
  "operation":    "add" | "replace" | "move" | "subtract",
  "format":       "auto" | "moxfield" | ...,                            // for source_text
  "dry_run":      true                                                  // for diff preview
}
```

Response when `dry_run=true`:

```jsonc
{
  "op_id":       "<uuid>",
  "summary": {
    "added":      [{"qty": 4, "name": "Sol Ring", "set": "cmr", ...}],
    "removed":    [...],
    "moved":      [{"name": "...", "from": "Box A", "to": "Box B", "qty": 1}],
    "promoted":   [{"name": "...", "from_provenance": "manual", "to_provenance": "mixed"}]
  },
  "warnings":     ["47 cards will be added without scan verification (provenance='manual')"],
  "unresolved":   ["The Tabarnacle of Worship Mountains  // typo, no Scryfall match"]
}
```

Response when `dry_run=false`: same shape, plus `applied: true`. Writes
inventory rows + inventory_history rows in one transaction. Returns
the `op_id` so future "undo last" can find the affected rows.

### `POST /api/bulk-edit/render-selection`

```jsonc
{
  "ids":    ["<scryfall_id>", ...],
  "format": "moxfield" | "plain" | ...
}
→ { "text": "..." }
```

Powers the floating-bar "Copy as Moxfield text" action and the bulk-edit
tab's "Pre-fill from selection" affordance.

### Operation rules

- **add** to inventory: creates rows, provenance='manual' or
  'moxfield_pull' depending on source.
- **add** to wishlist: pure list manipulation, no provenance concept.
- **replace**: error if any target row is `provenance='scan'` and
  would be removed without explicit `--allow-scan-loss=true`. Cards
  that survive keep their provenance; removed scanned cards become
  `provenance='gone'` rather than hard-deleted.
- **move**: only valid when source and target are physical (inventory
  boxes/binders) AND source rows are `provenance='scan'` or `'mixed'`.
  Updates `inventory.box`, appends `inventory_history` with
  `operation='move'`. No new rows created. Errors with
  `cards_not_scanned: [...]` listing offenders.
- **subtract**: removes rows by name from target. Same scan-loss rule
  as replace.

---

## UI

New tab `Bulk Edit` in the main nav, sibling to `Sort Session` and
`Collection`. Layout:

```
┌────────────────────────────┬────────────────────────────┐
│ Source: [▾ Box A         ] │ Target: [▾ Wishlist       ]│
│ Format: [▾ Moxfield      ] │                            │
│                            │                            │
│ <textarea, auto-populated  │ <textarea, auto-populated  │
│  from source list>         │  from target list>         │
│                            │                            │
│                            │                            │
│ [Reload from source]       │ [Apply →] [Replace] [Move] │
└────────────────────────────┴────────────────────────────┘

When [Apply], [Replace], or [Move] is clicked, a modal opens with:

  Diff preview
  + 47 cards added to Wishlist (manual entry — not scanned)
  − 12 cards removed from Wishlist
  → 8 cards moved from Box A to Wishlist (scanned, location update)

  WARNING: 47 cards will be added without scan verification.

  [Cancel]    [Apply this operation]
```

Source/target dropdowns share their option list:

- Collection (all, read-only as target)
- Box: <each>
- Wishlist
- Moxfield wishlist: <username>
- Moxfield deck: <id/url>
- Custom (empty textarea, source only)

When `source_kind` and `target_kind` are both physical-inventory, the
**Move** button is enabled. Otherwise it's disabled with tooltip
"Move requires both source and target to be physical locations."

---

## Selection-to-clipboard (Phase A.1)

Adds a checkbox column to the Collection inventory table. Selection
state lives in `sessionStorage` keyed by Scryfall UUID.

A floating bar at bottom-right activates the moment selection is
non-empty:

```
[ 47 cards selected · $312.40 total ]   [📋 Copy as Moxfield text ▾]   [Clear]
```

The dropdown on the copy button picks the format (Moxfield / plain /
MTGGoldfish / Archidekt / CSV / JSON / names-only). Clicking the button
itself uses the most-recent format. Clipboard is written via the standard
`navigator.clipboard.writeText` API.

That's the full feature on the output side. Pasting wherever — Moxfield,
the bulk-edit textarea, an email — is the OS's job.

---

## Phasing

### Phase A — v1 (target: 1 week of work)

- [ ] Schema migration (`provenance` column + `inventory_history` table)
- [ ] `moxfield_text.parse_text(text, format='auto')` with format detection
- [ ] `moxfield_text.render_text(entries, format=...)` multi-format
- [ ] `POST /api/bulk-edit/apply` with dry-run diff
- [ ] `POST /api/bulk-edit/render-selection`
- [ ] Bulk Edit tab UI (source dropdown, textarea, target dropdown,
      action buttons, diff modal)
- [ ] Checkbox column on Collection inventory table
- [ ] Floating selection bar with copy-format dropdown
- [ ] Tests:
  - parser correctly detects each format on representative samples
  - apply with each operation × each provenance combination
  - move rejects unscanned source rows
  - replace warns on scan loss
  - reconcile promotes provenance correctly

### Phase B — Safety + UX

- [ ] **Undo last bulk edit** (5-deep history per list, keyed on `op_id`)
- [ ] Watch-clipboard mode in Bulk Edit tab (auto-fills textarea on focus
      if clipboard contains valid card-list text)
- [ ] Keep `// UNRESOLVED:` rows in the textarea so typos don't get
      silently dropped
- [ ] Drag-and-drop CSV/TXT file → same parse pipeline

### Phase C — Box/divider abstraction

- [ ] `boxes(id, name, label_color, capacity, location_note)` table
- [ ] Migrate `inventory.box` → `inventory.box_id`
- [ ] Inline "Move to box…" action on inventory rows (one-card move
      without going through the Bulk Edit tab)
- [ ] Built-in "Cards needing a home" list (rows with `box_id IS NULL`)

### Phase D — Saved filters as lists

- [ ] `saved_filters(id, name, query_text)` table
- [ ] Saved filters appear as source/target options in Bulk Edit
- [ ] One-click "Save current filter as list" on the Collection page

### Phase E — Set operations + analysis

- [ ] Sub-tab in Bulk Edit: paste list A, paste list B, output A∩B / A∪B / A−B
- [ ] Deck-overlap report when target is a deck text: "you own 87/100"
- [ ] List-intersection from any two source dropdowns

### Phase F — Moxfield write-back (deferred)

Probably defer indefinitely. The textarea round-trip via Moxfield's own
Bulk Edit page covers 95% of value. If we ever take this on, we need:
- OAuth flow with Moxfield (no public write API)
- `sync_manifests` table per `plans/handoff/07_shared_interfaces.md`
- Conflict-resolution UI

---

## Open questions

- Should `provenance='gone'` rows be cleaned up after some retention
  window (e.g. 6 months) or kept forever for audit? Default to forever
  unless DB size becomes a concern.
- When pasting into the Bulk Edit textarea, do we auto-detect the
  format silently or show a small "detected: Moxfield" pill? Lean
  toward the pill for transparency.
- For "move" operations across boxes, should we emit a sort-session
  motion command that physically does the relocation? Out of scope
  for v1; the user moves cards manually after seeing the bulk edit.

---

## Cross-references

- Right-click menu: `plans/right_click_menu_plan.md` — its Add to →
  submenu writes through this plan's `POST /api/bulk-edit/apply`.
- Query helper: `plans/query_helper_plan.md` — its "show in collection"
  action navigates to the Collection page where this plan's checkboxes
  + selection bar live.
- Existing infra to reuse:
  - `web_enrichment/moxfield_text.py` (parser + renderer)
  - `web_enrichment/moxfield.py` (deck/wishlist fetch)
  - `collection_db.py` (inventory + wishlist + record_scan)
