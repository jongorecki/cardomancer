# Right-Click Menu Plan

## STATUS

Drafted 2026-04-27. Not started. Small enough to bundle with Bulk Edit
Phase A or land independently.

---

## Goal

Every card-row UI in the app — collection inventory table, sort-session
bin panels, detection-review table, future Moxfield deck preview, Bulk
Edit textarea (when row-mapped), query-helper card picker — gets the
SAME context menu. One menu, every list. The user learns it once.

The menu has two purposes:
1. **Look it up externally** — open the card on a relevant site.
2. **Categorize it** — add to a deck/binder/box/wishlist.

Per-card flag editing (toggle foil, set quantity, remove) lives only on
the sort session page where the cards are physically present mid-flow.
Everything else stays in the universal menu.

---

## The menu

```
Add to                    →  Deck • Binder • Box • Wishlist
───────────────
EDHREC
Scryfall
Scryfall Tagger
Store                     →  TCGplayer • Card Kingdom • Cardmarket
MTGStocks
Gatherer
───────────────
Copy name
Copy as Moxfield row
```

8 top-level items. The two submenus open on hover.

### URL builders

All external links are simple URL constructions from `(name, set, cn,
oracle_id)`. The set codes Scryfall uses match ours; TCG/CK use
proprietary slugs that we resolve via their search-by-name endpoints
rather than maintaining a mapping table.

| Link | URL pattern |
|---|---|
| EDHREC | `https://edhrec.com/cards/<slug-from-name>` |
| Scryfall | `https://scryfall.com/card/<set>/<cn>` |
| Scryfall Tagger | `https://tagger.scryfall.com/card/<set>/<cn>` |
| TCGplayer | `https://www.tcgplayer.com/search/magic/product?q=<urlencoded-name>+<set>` |
| Card Kingdom | `https://www.cardkingdom.com/catalog/search?search=mtg_advanced&filter[name]=<name>&filter[edition]=<set>` |
| Cardmarket | `https://www.cardmarket.com/en/Magic/Products/Search?searchString=<name>` |
| MTGStocks | `https://www.mtgstocks.com/search?q=<urlencoded-name>` |
| Gatherer | `https://gatherer.wizards.com/Pages/Card/Details.aspx?name=<urlencoded-name>` |

The "Add to" submenu writes through `POST /api/bulk-edit/apply` from
`plans/bulk_edit_plan.md` with `operation='add'`, `target_kind='inventory'`
(box/binder), `target_kind='wishlist'`, or a deck shell. No new API.

### Copy items

`Copy name` — literal card name, no quantity, no set.
`Copy as Moxfield row` — `1 Sol Ring (cmr) 472` formatted via
`render_text` on a one-element entry list.

Both write to the OS clipboard via `navigator.clipboard.writeText`.

---

## Implementation

### Frontend

One reusable component `<CardRowContextMenu>` mounted at the page
root. Each card-row registers itself on mount with:

```js
row.dataset.cardId = scryfallId;
row.dataset.cardName = name;
row.dataset.cardSet = set;
row.dataset.cardCn = collectorNumber;
```

A single document-level `contextmenu` listener walks up from the click
target to find the nearest element with `data-card-id` and opens the
menu positioned at the cursor. This means EVERY card row in the app
gets the menu by adding 4 data attributes — zero per-page wiring.

The menu component reads the data attributes, builds the URL set,
and renders the markup. Submenus are CSS hover-expand with a 150ms
intent delay (so the menu doesn't open submenus on transit).

### Mobile / touch

Long-press triggers the menu on touch devices. Same component.

### Keyboard

Right-click context-menu key (Shift+F10 / Menu key) supported via the
standard `contextmenu` event. Once open, arrow keys navigate, Enter
selects, Escape closes.

---

## Sort-session per-card flag editor

Distinct UI from the right-click menu. Lives in the bin panels we
already render in the sort session page.

Each `bin-card-entry` row gets a kebab `⋮` button on hover. Clicking
opens an inline popover with:

- **Toggle foil** ★  — flips `scan_history.is_foil` for this scan;
  re-emits `card_detected` so the UI updates. Backed by the same
  semantics as `apply_foil_verdicts.py`.
- **Set quantity** — small number input. Useful when one scan represents
  multiple physical copies (e.g. you scanned a 4-of by accident, want
  the inventory to reflect 4 copies from this single scan).
- **Remove from session** — deletes the `scan_history` row for this
  scan_num, decrements the bin count. Useful when a misread keeps
  appearing in the bin list.

**Not included** (intentional):

- ~~Move to bin~~ — by the time the user wants to relocate a card, it's
  several cards deep in the bin. Physically impractical. If a card was
  routed to the wrong bin, the right fix is "Remove from session" then
  re-scan into the correct bin manually.

---

## Phasing

### Phase A — v1 (target: 2-3 days)

- [ ] `<CardRowContextMenu>` React/vanilla component
- [ ] Document-level `contextmenu` listener with data-attribute walk
- [ ] URL builders for all 8 external links
- [ ] "Add to" submenu wired to `POST /api/bulk-edit/apply`
- [ ] Copy actions via `navigator.clipboard.writeText`
- [ ] data-card-* attributes added to:
  - [ ] Collection inventory table rows
  - [ ] Sort-session bin-panel rows
  - [ ] Detection-review table rows
- [ ] Sort-session per-card flag editor (kebab popover)
- [ ] Tests:
  - menu appears on right-click of any card row
  - each external link opens the right URL
  - "Add to" successfully creates an inventory_history entry
  - flag editor toggles foil and persists

### Phase B — Polish

- [ ] "More links →" submenu for the long-tail external sites
      (Cardmarket, MTGGoldfish, EDHTOP16, MTGAzone, etc.) — kept
      hidden by default so the top level stays terse
- [ ] Recent/favorite "Add to" targets pinned to the top of the
      submenu (most-recent-deck, most-recent-box)
- [ ] Right-click on a multi-row selection: same menu, but actions
      apply to the whole selection (single call to bulk-edit/apply
      with all IDs)

### Phase C — App-internal navigation (deferred)

Considered for v1 but cut to keep the menu tight. Reconsider when
genuine demand emerges:

- "Show in collection" — jumps to inventory tab with the row scrolled
  into view and highlighted
- "Show all printings I own" — filters inventory by `oracle_id:<this>`
- "Show scan history" — opens a per-card scan history modal
- "View my scan / Compare scan vs reference" — image diff tool

These all read well as a "Show →" submenu if/when added. The data is
already available; the cost is purely UI.

---

## Open questions

- For the URL builders, do we want an "Open all" option that fires
  every link in tabs at once? Useful for a deep dive on one card.
  Can be a Phase B addition if requested.
- For "Add to → Box", do we list boxes alphabetically or by recent
  use? Recent use likely better; the menu ordering is one-liner UX
  config.

---

## Cross-references

- Bulk Edit: `plans/bulk_edit_plan.md` — `POST /api/bulk-edit/apply`
  is the backend for the "Add to" submenu.
- Query helper: `plans/query_helper_plan.md` — its card picker uses
  the same row component, so the menu just works there too.
- Existing infra to reuse:
  - `static/app.js _showCardPreview` — hover preview pattern
  - `web_enrichment/moxfield_text.py render_text` — copy-as-Moxfield
