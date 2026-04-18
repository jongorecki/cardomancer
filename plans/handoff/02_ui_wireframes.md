# UI Wireframes

ASCII sketches of every new view. Match existing Bootstrap 5 layout conventions in `templates/index.html` and the CSS variables from `static/style.css`. Keep the dark theme as default, ensure light theme works via the `[data-theme="light"]` overrides.

Common layout rules:
- Navbar: unchanged (E-stop, connection/state/camera badges, theme toggle)
- Tabs: 4 total (Dashboard, Bin Setup, Sort Session, Collection). Motion Preview + Calibration + Database removed as top-level tabs.
- Calibration becomes a modal launched from Dashboard.
- Database becomes a Dashboard subsection ("Data & Sources" card).

---

## Tab 1: Dashboard (revised)

```
┌────────────────────────────────────────────────────────────────────────┐
│ [Hardware]             │ [Camera]                                      │
│   COM3  Connected      │   Active  32 FPS                              │
│   [Connect][Home All]  │   [Start][Stop]                               │
│   ─────                │   ┌────── live mjpeg ──────┐                  │
│   [Home X][Home Z]     │   │                       │                  │
│                        │   └───────────────────────┘                  │
├────────────────────────┴───────────────────────────────────────────────┤
│ [Activity Log]                                                         │
│   14:02  Session #47 started (color sort, 6 bins)                      │
│   14:03  Scanned "Sol Ring"  → bin 4  (ramp)                           │
│   ...                                                                  │
├────────────────────────────────────────────────────────────────────────┤
│ [Data & Sources]                    [Calibration]                      │
│  Scryfall bulk   ✓ fresh 2h ago      Current offsets:                  │
│  Tagger          ⚠ 98.7% coverage     CAMERA_X = 100.2mm               │
│  EDHREC          ✓ fresh 5d          Last calibrated: 2026-04-10        │
│  edhtop16        ✓ fresh 5d          [Launch Calibration Wizard]        │
│  Spellbook       ✓ fresh 2d                                             │
│  CK buylist      ✓ fresh 18h                                            │
│  [Refresh all]  [Refresh prices]                                        │
│  [Run probes]                                                           │
└────────────────────────────────────────────────────────────────────────┘
```

- Activity log persists existing behavior.
- Data & Sources: one row per source. Status icon, coverage %, last-success timestamp. Row click → drawer with last refresh log + manual refresh button for that source.
- Calibration card: current offsets, launch wizard button → modal (see Calibration Wizard below).

---

## Tab 2: Bin Setup (unchanged structure)

No wireframe changes from current. Ensure preset save/load integrates with the unified preset store.

---

## Tab 3: Sort Session

### 3a. Session start panel (top of tab)

```
┌────────────────────────────────────────────────────────────────────────┐
│ New session                                                            │
│                                                                        │
│ Preset:  [ Color by CMC       ▼ ]  [Save as...] [Edit]                 │
│                                                                        │
│ Bin configuration (editable; changes here don't alter the preset)      │
│ ┌────┬────────────────────────────────────────────────┬──────────┐    │
│ │ #  │ Scryfall query                                 │ Est.     │    │
│ ├────┼────────────────────────────────────────────────┼──────────┤    │
│ │ 1  │ c:w cmc<=2                                     │ 2,340    │    │
│ │ 2  │ c:u cmc<=2                                     │ 1,890    │    │
│ │ 3  │ c:b cmc<=2                                     │ 2,104    │    │
│ │ 4  │ c:w cmc>=3                                     │ 3,002    │    │
│ │ 5  │ c:u cmc>=3                                     │ 2,755    │    │
│ │ 6  │ (fallback)                                     │ —        │    │
│ └────┴────────────────────────────────────────────────┴──────────┘    │
│                                                                        │
│ Storage plan:  Box [box-3 ▼]  starting divider [ 15 ] [New box]        │
│                                                                        │
│ [  START SESSION  ]                                                    │
└────────────────────────────────────────────────────────────────────────┘
```

Key interactions:
- Every preset loads into the same table. "Load custom from file" removed.
- "Est." column shows estimated card count from a dry-run count against owned inventory (quick query, cached).
- Edit a row inline → preset becomes "(modified)" with a Save As button.
- Storage plan section is new. Required before session starts.

### 3b. Active session panel

```
┌────────────────────────────────────────────────────────────────────────┐
│ Session #48  │  Cards scanned: 142  │  Rate: 8.2 c/min  │  [Pause]     │
├─────────────────────────────────────┬──────────────────────────────────┤
│  Live camera feed                   │  Last scanned                    │
│  ┌───────────────────────┐          │  ┌───────────────────────────┐   │
│  │                       │          │  │   [card face image]       │   │
│  │                       │          │  │                           │   │
│  └───────────────────────┘          │  └───────────────────────────┘   │
│                                     │  Lightning Bolt                   │
│  [Undo last] [Detect single]        │  {R} • Instant                    │
│                                     │  $1.50 • CK buylist: $0.35       │
│                                     │  Tags: removal, burn             │
├─────────────────────────────────────┤  Salt: 0.9  •  In 3 of your decks│
│  Bin status                         │  ★ Wishlist match!                │
│   1 ▓▓▓▓▓▓░░░░  12/25 (c:w cmc≤2)  │                                  │
│   2 ▓▓▓░░░░░░░   4/25 (c:u cmc≤2)  │                                  │
│   3 ▓▓▓▓▓▓▓▓░░  18/25 (c:b cmc≤2)  │                                  │
│   4 ▓░░░░░░░░░   1/25 (c:w cmc≥3)  │                                  │
│   ...                               │                                  │
└─────────────────────────────────────┴──────────────────────────────────┘
```

- **Live card info panel (right)** is new. Shows enrichment data for last scan.
- Wishlist match = star + yellow highlight + notification.
- Bin status bars show current fill vs capacity; label shows active query.

---

## Tab 4: Collection (hybrid layout per your choice)

### Top-level layout

```
┌────────────────────────────────────────────────────────────────────────┐
│ [Inventory] [Locator] [Sync]                                           │
│  (default pill-style sub-view selector; inventory is the default)      │
└────────────────────────────────────────────────────────────────────────┘
```

### 4a. Inventory (default view)

```
┌──────────────────┬─────────────────────────────────────────────────────┐
│ FILTERS          │ Search:  [ scryfall-style query ... ]  [Apply]      │
│                  │                                                      │
│ Cull preset:     │ View mode:  (•) Table  ( ) Grid                      │
│  [ ] Default     │ Sort: [Name ▼] [Price ▼] [Location ▼]                │
│  [ ] Strict      │                                                      │
│                  │ Showing 1,234 of 8,920 cards                         │
│ Tags:            │ ┌────────────────────────────────────────────────┐  │
│  [x] removal     │ │ Name            Set  Qty  Price  Loc     Tags  │  │
│  [ ] ramp        │ │ Lightning Bolt  m10   4   $1.50  b3/d14  burn  │  │
│  [x] card-draw   │ │ Swords to Pl.   lea   1   $14    b3/d15  rem.  │  │
│  [ ] counterspl  │ │ ...                                            │  │
│  (show all…)     │ └────────────────────────────────────────────────┘  │
│                  │                                                      │
│ Staples:         │ [Export CSV] [Export decklist] [Cull selected]      │
│  [x] Universal   │                                                      │
│  [ ] Archetype   │                                                      │
│  [ ] cEDH        │                                                      │
│                  │                                                      │
│ Price:           │                                                      │
│  [min] - [max]   │                                                      │
│                  │                                                      │
│ Salt:            │                                                      │
│  [====|====]     │                                                      │
│                  │                                                      │
│ Deck usage:      │                                                      │
│  ( ) any         │                                                      │
│  (•) in a deck   │                                                      │
│  ( ) not in any  │                                                      │
│                  │                                                      │
│ Has buylist:     │                                                      │
│  ( ) either      │                                                      │
│  (•) yes         │                                                      │
│  ( ) no          │                                                      │
│                  │                                                      │
│ Box:             │                                                      │
│  [ All ▼ ]       │                                                      │
│                  │                                                      │
│ [Clear filters]  │                                                      │
└──────────────────┴─────────────────────────────────────────────────────┘
```

- Filter sidebar collapsible (chevron at top). All filters compose (AND).
- Search box accepts Scryfall + enrichment syntax; filters and search box are additive.
- Cull preset buttons apply a bundle of filters at once.
- Row click → side drawer with full card detail (same payload as `/api/enrichment/card/<id>`).
- "Cull selected" generates a one-bin sort preset from checked rows.

### 4b. Locator view

```
┌──────────────────┬─────────────────────────────────────────────────────┐
│ FILTERS (same    │ Locate: [ otag:removal color:w ... ]  [Find]        │
│   sidebar)       │                                                      │
│                  │ 14 cards matched across 2 boxes, 3 dividers:         │
│                  │                                                      │
│                  │  ┌──────────────────────────────────────────┐        │
│                  │  │ 📦 box-3 "Modern commons"                │        │
│                  │  │   └─ divider 14  (6 cards)               │        │
│                  │  │       Swords to Plowshares ×1            │        │
│                  │  │       Path to Exile ×2                   │        │
│                  │  │       ...                                │        │
│                  │  │   └─ divider 15  (5 cards)               │        │
│                  │  │       ...                                │        │
│                  │  │                                          │        │
│                  │  │ 📦 box-5 "Commander cards"               │        │
│                  │  │   └─ divider 22  (3 cards)               │        │
│                  │  │       ...                                │        │
│                  │  └──────────────────────────────────────────┘        │
│                  │                                                      │
│                  │ [Generate pick list] [Re-sort these cards]           │
└──────────────────┴─────────────────────────────────────────────────────┘
```

- Same filter sidebar as Inventory.
- Grouped by box → divider. Collapsible groups.
- "Generate pick list" produces a printable sheet (label.html template reused).
- "Re-sort these" creates a sort preset that pulls only these cards in the next session.

### 4c. Sync view

```
┌──────────────────────────────────────────────────────────────────────┐
│ Moxfield                                                             │
│                                                                      │
│ Pull from deck:                                                      │
│  [ https://moxfield.com/decks/abc123 ...           ] [Preview]       │
│                                                                      │
│  Printing mode:  ( ) Exact  (•) Any  ( ) Prefer listed               │
│  Target bins:    (•) Single bin  ( ) By category  ( ) By color       │
│  [Create sort preset]                                                │
│                                                                      │
│ Pull binder as wishlist:                                             │
│  [ moxfield binder URL ...                         ] [Import]        │
│                                                                      │
│ Push (deferred) — requires login, not yet enabled                    │
│                                                                      │
│ ─────                                                                │
│                                                                      │
│ Recent imports:                                                      │
│   ✓ 2026-04-18  "Marchesa aristocrats"  → preset_moxfield_marchesa   │
│   ✓ 2026-04-17  "Wants binder"          → 34 wishlist items          │
└──────────────────────────────────────────────────────────────────────┘
```

---

## Modals

### Calibration Wizard (launched from Dashboard)

```
┌────────────────────────────────────────────────────────────────────┐
│ Calibration Wizard                                    [x]          │
├────────────────────────────────────────────────────────────────────┤
│ Step 2 of 5: Camera X offset                                       │
│                                                                    │
│ Place an ArUco marker directly below the suction head, then press  │
│ Detect.                                                            │
│                                                                    │
│ ┌── camera feed with overlay ────────┐                             │
│ │  [marker with crosshair drawn]     │                             │
│ │  offset: +0.4mm → -0.1mm           │                             │
│ └────────────────────────────────────┘                             │
│                                                                    │
│ Previous:  100.2mm                                                 │
│ Measured:  100.6mm      [Detect] [Accept] [Skip]                   │
│                                                                    │
│ [← Back]                         [Regression check] [Next →]       │
└────────────────────────────────────────────────────────────────────┘
```

### Enrichment Source Detail (drawer from Dashboard)

```
┌────────────────────────────────────────────────────────────────────┐
│ Tagger (Scryfall)                                       [x]        │
├────────────────────────────────────────────────────────────────────┤
│ Status: ⚠ degraded                                                 │
│ Last success: 2026-04-16 10:12 (2d ago)                            │
│ Coverage: 98.7% (793/803 tags)                                     │
│                                                                    │
│ Issues:                                                            │
│  • "removal" expected 1850 cards, got 1847                         │
│  • "ramp" expected 640, got 638                                    │
│  • "mana-rock" expected 104, got 103                               │
│                                                                    │
│ [Re-fetch missing] [Run probe] [Refresh all]                       │
│                                                                    │
│ Recent log:                                                        │
│  2026-04-16 10:12  refresh started                                 │
│  2026-04-16 10:34  refresh complete (22m)                          │
│  2026-04-16 10:34  WARN: 3 tag counts mismatched                   │
└────────────────────────────────────────────────────────────────────┘
```

### Card Detail Drawer (click any card anywhere)

```
┌────────────────────────────────────────────────────────────────────┐
│ [card image]      Lightning Bolt                            [x]    │
│                                                                    │
│ {R} • Instant                                                      │
│ Lightning Bolt deals 3 damage to any target.                       │
│                                                                    │
│ Prices: market $1.50  ·  foil $8.00  ·  CK buylist $0.35          │
│                                                                    │
│ Tags: removal, burn, damage-direct                                 │
│ Art tags: dragon (ema), ...                                        │
│ Staples: Universal ✓  Archetype (in 38)  cEDH ✓                    │
│ Salt: 0.9                                                          │
│ Combos: none                                                       │
│ In your decks: Kroxa aggro, Obosh burn, RDW legacy                 │
│                                                                    │
│ Inventory:                                                         │
│   M10 nonfoil ×4  (box-3 / divider 14)                             │
│   BRR foil ×1    (box-1 / divider 2)                               │
│                                                                    │
│ [Edit location] [Add to wishlist] [Mark for cull]                  │
└────────────────────────────────────────────────────────────────────┘
```

---

## Mobile considerations (future)

Current wireframes are desktop-first. When mobile view is built, collapse the filter sidebar to a drawer icon and stack panels vertically. Tables should horizontal-scroll rather than reflow.
