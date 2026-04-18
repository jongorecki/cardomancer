# Acceptance Criteria

"Done looks like..." for every feature in the plan. Each bullet is a testable claim. Feature is not complete until every bullet passes.

---

## Phase 0: Foundation

### Unified sort preset UI
- [ ] All presets (builtin + user-saved) render in the same table layout with one row per bin showing `bin #`, `query`, `description`.
- [ ] Editing a bin query inline marks the preset as modified; "Save As" persists as a new user preset.
- [ ] Deleting "Load custom from file" removes only the button; backing logic for loading from `sort_configs/*.txt` still works (now auto-imported as read-only presets on startup).
- [ ] Legacy preset names (`color`, `price`, etc.) still resolve to their expected bin layouts.
- [ ] Estimated card count per bin populates within 500ms for a 10-bin preset against a 10k-card inventory.
- [ ] Storage plan controls (box dropdown, starting divider) appear on the session start panel and are required before Start becomes enabled.

### enrichment.db schema + migrations
- [ ] New file `enrichment.db` created alongside `collection.db`.
- [ ] All tables from `shared_interfaces.md` created with expected columns and indices.
- [ ] Migrations script is idempotent (running twice leaves the DB in identical state).
- [ ] Seed rows exist for every `oracle_id` present in Scryfall bulk data.
- [ ] Cross-DB queries (collection LEFT JOIN enrichment) return correct row counts against a fixture dataset.

### Refresh scheduler
- [ ] APScheduler integrated into `web_server.py`; starts with the server.
- [ ] Weekly job triggers for each enrichment source (placeholder functions OK in Phase 0).
- [ ] Daily job triggers for prices.
- [ ] Manual refresh endpoint `POST /api/enrichment/refresh/<source>` returns 202 and emits `enrichment_refresh_progress` events.
- [ ] `sync_metadata` rows update on every refresh attempt (success or failure).

### Probe scripts
- [ ] `probes/probe_<source>.py` exists for each source listed in `web_enrichment_source_probes.md`.
- [ ] Each probe exits 0 on shape match, non-zero on drift.
- [ ] Pinned snapshots live under `tests/probe_snapshots/<source>_pinned.json`.
- [ ] `probes/run_all.py` runs every probe and reports a summary.
- [ ] Running `run_all.py` fresh after `git clone` + install succeeds for every source marked ✅ or 🟡 (Tagger may require adjustment if GraphQL shape changed since probes were pinned).

### Motion Preview hidden
- [ ] Navbar tab list shows 4 tabs: Dashboard, Bin Setup, Sort Session, Collection.
- [ ] Motion Preview JS/HTML files remain in the repo, unmodified.
- [ ] A commented block in `index.html` indicates where to re-insert the tab.

### Tab consolidation
- [ ] Calibration tab content moved into a modal launched from Dashboard; all existing calibration endpoints still work unchanged.
- [ ] Database tab content moved into Dashboard "Data & Sources" card; all existing DB update endpoints still work.
- [ ] Collection tab shows sub-view pills for Inventory / Locator / Sync.

---

## Phase 1: Annotation layer

### Scryfall Tagger cache
- [ ] `probe_tagger.py` passes (if Tagger GraphQL reachable) or documented fallback path is active.
- [ ] `tags` table populated; `atag` table populated.
- [ ] `tag_catalog` populated with hierarchy + expected counts.
- [ ] For every tag, `actual` card count in `coverage_reports` is within 1% of `expected`, OR flagged in `sync_metadata` as degraded.
- [ ] Spot-check: 50 random owned cards, tags in local cache match Tagger website responses.
- [ ] Tag hierarchy query (`otag:removal` rolls up children) returns correct superset.
- [ ] Weekly refresh only re-pulls changed tags (measured against baseline run).

### EDHREC staples aggregator
- [ ] Inclusion rates pulled for top 1000 cards.
- [ ] `staples` table has rows for every card with `inclusion > 5%` tagged as `staple:universal`.
- [ ] Archetype top lists pulled from ≥ 100 theme/tribe pages.
- [ ] Cards appearing in top 100 of ≥ 3 archetypes tagged as `staple:archetype` with `archetypes_json` populated.
- [ ] Salt scores captured for every card page pulled.
- [ ] Sol Ring shows `staple:universal == true`, `staple:cedh == true` (once edhtop16 runs), `salt > 1`.

### edhtop16 cEDH staples
- [ ] GraphQL introspection succeeded or query built from empirical snapshots.
- [ ] 6-month rolling window of top-16 decks pulled.
- [ ] Inclusion rate computed; `staple:cedh` tier populated for cards above 15% threshold.
- [ ] Basic lands explicitly excluded from cEDH staple output.
- [ ] Thassa's Oracle appears with `staple:cedh == true`.

### Commander Spellbook combos
- [ ] `combos` and `combo_membership` tables populated.
- [ ] Combo count within 1% of Spellbook's reported total.
- [ ] Thassa's Oracle + Demonic Consultation appears as a combo.

---

## Phase 2: Collection tooling

### Physical locator
- [ ] `POST /api/locator/query` returns correct box/divider groupings for a known test set.
- [ ] Accepts full Scryfall + enrichment query syntax.
- [ ] Aggregates cards by (box, divider); sorted by box_id then divider_id.
- [ ] Returns `total_cards` and per-group `count` fields accurately.

### Divider / box tracking
- [ ] A sort session with a storage plan auto-increments divider as bins fill.
- [ ] `storage_divider_advanced` event emitted when bin cap reached.
- [ ] Every scan recorded during a planned session has a populated `storage_locations` row.
- [ ] Post-session summary endpoint returns both JSON and printable HTML (via label.html).
- [ ] Manual location edit endpoint updates a card's location and sets `confidence='manually_edited'`.
- [ ] Location queries return `confidence` field on every result.

### Tag filters in Collection
- [ ] Filter sidebar shows tag chips populated from `tag_catalog`, sorted by usage count.
- [ ] Selecting a chip combines with existing query (AND semantics).
- [ ] Staple-tier checkboxes filter correctly.
- [ ] Salt range slider filters by `salt_scores.salt`.
- [ ] Clearing all filters restores default view.

### Dead-weight cull view
- [ ] `GET /api/collection/cull-candidates?preset=default` returns cards matching: (vanilla OR french-vanilla) AND no-staple AND no-buylist > $0.05 AND not-in-any-linked-deck.
- [ ] `cull_reasons` populated per card (e.g., `["vanilla", "no_buylist", "not_in_decks"]`).
- [ ] `strict` preset additionally requires market price == 0.
- [ ] "Cull selected" generates a sort preset with the selected cards as a one-bin target.
- [ ] Known-vanilla test cards (e.g., Grizzly Bears, Squire) appear in default cull results.

---

## Phase 3: Integrations

### Moxfield deck import
- [ ] Public deck URL → valid sort preset created.
- [ ] `printing_mode: any` matches cards by `oracle_id` only.
- [ ] `printing_mode: exact` matches by `(set, collector_number)` only.
- [ ] `printing_mode: prefer_listed` returns a two-pass preset: first bin queries by specific printing, second bin by oracle_id.
- [ ] `target_bin_strategy: single_bin` produces a one-bin preset.
- [ ] `target_bin_strategy: by_category` splits into creatures / spells / lands / commanders bins.
- [ ] Deck cache hits on second call within same session.
- [ ] Probe catches Moxfield shape drift before a broken import can happen.

### Moxfield binder import
- [ ] Public binder → wishlist entries created.
- [ ] Duplicate detection: running twice does not create duplicate wishlist entries.
- [ ] `merge_with_existing: false` replaces rather than merges.

### Buylist integration (CardKingdom)
- [ ] Daily refresh pulls CK buylist into `buylists` table.
- [ ] Every card with a non-zero CK buylist price has a row.
- [ ] Cards without a buylist entry return empty array (not null) on `/api/enrichment/card/<id>`.

### Deck usage overlay data
- [ ] Pulling all user Moxfield decks populates `deck_usage` linkage.
- [ ] "Used in N decks" badge on live card info matches the count.

---

## Phase 4: UI polish

### Live card info overlay
- [ ] Panel appears on Sort Session tab when a session is active.
- [ ] Content updates within 200ms of `card_detected` SocketIO event.
- [ ] Shows: card image, name, mana cost, type line, oracle text, prices (market + buylist), tags, salt, deck usage, staples status.
- [ ] Wishlist hit shows a visible star + yellow highlight.

### Session gallery + analytics
- [ ] Sessions list shows aggregate stats (cards, duration, rate, recognized %).
- [ ] Session detail shows every scan with thumbnail.
- [ ] Grid view supports filters (bin, confidence, foil, date range).
- [ ] Charts: cards per bin, cards per minute over time, confidence histogram.

### Wishlist priority bin + notifications
- [ ] Priority bin always wins routing over other queries.
- [ ] Browser notifications fire on wishlist match (with user permission prompt).
- [ ] Discord webhook test posts successfully.
- [ ] ntfy webhook test posts successfully.

### Calibration wizard
- [ ] All 18 existing calibration endpoints accessible via wizard flow.
- [ ] Before/after overlay renders on the live camera feed.
- [ ] Regression check compares current offsets to stored baseline; flags > 1mm drift.
- [ ] Wizard can be cancelled mid-flow without corrupting calibration state.

---

## Cross-cutting

### Verification artifacts
- [ ] Every external source has: probe script, schema snapshot test, Pydantic model, unit tests, coverage check.
- [ ] `tests/enrichment/test_cross_source_sanity.py` passes with Sol Ring, Grizzly Bears, Thassa's Oracle, Squire cases.
- [ ] Running the full test suite takes < 60s (excluding live network probes).
- [ ] `probes/run_all.py` exits 0 on a freshly cloned + installed repo (given network access).

### Performance
- [ ] Collection tab initial load < 500ms against a 10k-card inventory.
- [ ] Filter changes reflow the table in < 200ms.
- [ ] Enrichment query against full corpus returns first page in < 300ms.

### Resilience
- [ ] Server starts and core sorting flow works with `enrichment.db` empty.
- [ ] Server starts and enrichment features degrade gracefully when a source is stale.
- [ ] Probe drift aborts a refresh but does not take down the server.

### Documentation
- [ ] New README section in `plans/` pointing to this handoff set.
- [ ] Every new module has a top-of-file comment describing its purpose (matches existing style).
- [ ] Any new sort-query syntax (e.g., `staple:`, `salt>`, `combo:`) is documented in `query_parser.py` docstring.
