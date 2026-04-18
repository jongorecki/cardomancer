# Web Enrichment & Integrations Plan

Scope: extend the card-sorter web server with external data integrations (EDHREC, edhtop16, Scryfall Tagger, Commander Spellbook, Moxfield, buylists), a physical-location tracking system, unified/transparent sort presets, and a collection-management layer that treats the full Scryfall corpus (not just owned cards) as queryable data.

This is a planning document. Implementation happens in a separate chat.

---

## Goals

- Every sort preset is **transparent**: UI shows the Scryfall query per bin before you hit start.
- "Get rid of cards I'll never use" becomes a real, data-backed action.
- Collection tab works as a physical-card locator once scans are in.
- Enrichment data covers **all ~30k Scryfall oracle cards**, not just owned, so features extend to wishlists, trades, and deck-building.
- Every external data source has explicit verification and a plan for when it breaks.

## Non-goals

- Rebuilding the scan/detection pipeline.
- Multi-user auth (deferred).
- Live dashboards for deck-drafting / playtesting (deferred).
- Motion Preview tab (hidden; code retained).

---

## Data sources

| Source | Endpoint shape | Stability | Rate limit | What we extract |
|---|---|---|---|---|
| Scryfall bulk data | Official JSON dump | Official, very stable | Daily | Card metadata, oracle_id ↔ printing_id, prices |
| Scryfall search API | REST `otag:` / `atag:` | Official, stable | 50-100ms | Cards per tag, resolves tag hierarchy |
| Scryfall Tagger | **Undocumented** GraphQL at `tagger.scryfall.com/graphql` | Community-used, no guarantees | Unknown | Tag catalog, hierarchy, descriptions |
| EDHREC | Undocumented JSON at `json.edhrec.com/pages/...` | Widely used, stable in practice | Unknown (be gentle) | Inclusion rates, themes, salt scores, combos, commander popularity |
| edhtop16 | GraphQL at `edhtop16.com/api/graphql` | Public, intended for query | Unknown | cEDH tournament decklists, top finishes |
| Commander Spellbook | REST API, documented | Official API | Documented | Combo database |
| Moxfield | Unofficial `api2.moxfield.com/v2/...` | Fragile | Unknown | Deck lists, binders, collections |
| Buylists | Vendor-specific (TCGPlayer, CK, CardConduit) | Varies | Varies | Per-card buylist price |
| strictlybetter.eu | GitHub JSON dump | Community project | n/a (download) | Dominated-by relationships (deferred) |
| MTG Goldfish movers | RSS feed | Public RSS | n/a | Weekly price movers (optional) |

**Critical dependency check before building:** Tagger GraphQL, EDHREC JSON, and Moxfield are all undocumented. Phase 0 must include a probe script for each that proves the endpoint exists and documents its response shape as we observe it.

---

## Storage schema

**`enrichment.db`** (new SQLite, keyed on full Scryfall corpus, ~30k oracle IDs):

```
tags               (oracle_id, tag_name, source)           -- otag cache
art_tags           (printing_id, tag_name, source)         -- atag cache
tag_catalog        (tag_name, parent, description, card_count_expected, source)
staples            (oracle_id, tier, source, score, archetypes_json)
   -- tier ∈ {universal, archetype, cedh}
   -- source ∈ {edhrec, edhtop16}
salt_scores        (oracle_id, salt, last_updated)
themes             (oracle_id, theme_name, source, inclusion_pct)
combos             (combo_id, cards_json, result, source)
combo_membership   (oracle_id, combo_id)
commander_ranks    (oracle_id, deck_count, avg_synergy_json, source)
buylists           (oracle_id, vendor, price_usd, last_updated)
price_history      (oracle_id, date, market_usd, source)
local_tags         (oracle_id, tag_name)                   -- manual overrides
sync_metadata      (source, last_success, last_attempt, version_hash, error)
coverage_reports   (source, run_at, tag_or_key, expected, actual, diff)
```

**`collection.db` additions** (extends existing):

```
storage_locations  (scan_id, box_id, divider_id, position, confidence, notes, updated_at)
   -- confidence ∈ {robot_placed, manually_edited, resort_moved, stale}
storage_sessions   (session_id, box_id, starting_divider, ending_divider, created_at)
sync_manifests     (target, oracle_id, qty, foil_qty, condition, last_uploaded_at)
   -- target ∈ {moxfield, archidekt}
```

All enrichment queries against owned cards are `collection.db` LEFT JOIN `enrichment.db ON oracle_id` — clean, no foreign keys across DBs needed.

---

## Phases

### Phase 0 — Foundation (do first, unblocks everything else)

1. **Unified sort preset UI**
   - Every preset loads into the same layout as the Custom tab: one row per bin showing editable Scryfall query + human-readable description.
   - Remove "Load from file" from Sort Options (duplicate of Custom tab).
   - "Save as preset" button so the displayed config can be named and stored.
   - Preset list = same backing store as today, just a different load entry point.

2. **`enrichment.db` schema + migrations**
   - Create tables above. Migrations script that handles re-runs safely.
   - Seed with placeholder rows for every `oracle_id` in Scryfall bulk, so JOINs never miss.

3. **Refresh scheduler**
   - APScheduler inside `web_server.py`.
   - Weekly cron for enrichment sources; daily cron for prices.
   - Each source writes `sync_metadata` row on completion.
   - Manual "Refresh now" endpoint per source; progress emitted via SocketIO.

4. **Probe scripts for undocumented sources**
   - `probe_tagger.py`, `probe_edhrec.py`, `probe_moxfield.py`
   - Each hits its endpoint, asserts response shape, writes a pinned example response to `tests/probe_snapshots/`.
   - Run as part of pre-refresh hook: if shape drifts from pinned snapshot, refresh aborts and alerts user instead of silently corrupting data.

5. **Hide Motion Preview tab**
   - Comment out nav entry, retain JS/HTML so it can be re-enabled.

### Phase 1 — Annotation layer

6. **Scryfall Tagger cache** (see dedicated section below)
7. **EDHREC staples aggregator**
   - Pull inclusion rates from `json.edhrec.com/pages/top/*.json` equivalents.
   - Pull archetype/theme top-card lists.
   - Derive tiered staple labels (universal, archetype, niche) + `archetype_count`.
8. **edhtop16 cEDH staples**
   - GraphQL query for top-N finishing decks, rolling 6-month window.
   - Compute inclusion rate across that corpus → `staple:cedh` tier.
9. **Commander Spellbook combos**
   - Pull combos, store combo + membership tables.
10. **Salt scores, themes, commander popularity** (EDHREC auxiliary data)

### Phase 2 — Collection tooling

11. **Physical locator**
    - Query box: takes any Scryfall syntax query (including `otag:`, `tier:`, `combo:` from enrichment fields).
    - Returns owned cards matching + their `storage_location`.
    - Aggregates by (box, divider) range.
12. **Divider / box tracking**
    - Per-sort-session storage plan: "append to box N, starting at divider M."
    - Worker auto-increments divider as each bin reaches capacity.
    - Post-sort summary shows divider range → printable box label (tie into existing label template).
    - `confidence` field tracks whether a location is trusted.
13. **Tag filters in Collection tab**
    - Sidebar chips for `otag:` categories (removal, ramp, card-advantage, etc.).
    - Combined with staple-tier filters, salt, price range.
    - Deep-links to physical locator result for any filter.
14. **Dead-weight cull view**
    - Preset: `otag:vanilla OR otag:french-vanilla` OR (`staple_score == 0 AND not in any linked deck AND buylist < $0.05`).
    - "Move to cull bin" button generates a one-bin sort config for the next physical pass.
    - `strictlybetter.eu` integration deferred to a later drop-in.

### Phase 3 — Integrations

15. **Moxfield pull**
    - Deck URL → sort bin preset with printing-mode toggle (exact / any / prefer-listed).
    - Binder URL → wishlist import.
    - Collection import → seed `collection.db` (with dry-run preview).
16. **Moxfield push (diff-based sync)**
    - `sync_manifests` table tracks last-uploaded state per oracle_id.
    - Pending-changes counter in Collection tab with detail view.
    - One-click push; Moxfield CSV format.
17. **Buylist integration**
    - TCGPlayer, CardKingdom, CardConduit buylists.
    - Daily refresh alongside prices.
    - Enables real dead-weight detection and a "vendor submission pile" sort mode.
18. **Moxfield deck-usage overlay data**
    - Pull all your Moxfield decks → store which cards appear in which.
    - Drives the "used in N decks" badge.

### Phase 4 — UI polish & consolidation

19. **Live card info overlay during sort**
    - Right-side panel: card face, oracle text, price, buylist, deck usage, synergy score, MTGStocks trend (if enabled), wishlist flag.
    - Swaps in for each new scan.
20. **Session gallery + analytics**
    - Grid of every scan with filters (bin, date, confidence, foil, set).
    - Per-session stats: cards/hour, bin distribution, detection confidence histogram.
21. **Wishlist priority bin + notifications**
    - Bin that always wins routing.
    - Browser notifications + optional webhook (Discord/ntfy) on wishlist hit.
22. **Calibration wizard**
    - Single guided flow wrapping the 18 calibration endpoints.
    - Before/after camera-feed screenshots, visual offset overlays, regression-check pass.
23. **Tab consolidation**
    - Target: 7 → 4 tabs.
    - **Dashboard** absorbs Database and Calibration as sub-modals/sections.
    - **Bin Setup** unchanged.
    - **Sort Session** unchanged (houses live card info overlay).
    - **Collection** absorbs wishlist, locator, tag filters, cull view, sync controls.
    - Motion Preview hidden (phase 0).

---

## Scryfall Tagger pull — detailed procedure

**Why this gets its own section:** the Tagger GraphQL endpoint is undocumented. We need to prove each assumption before relying on it.

### Assumptions to verify (Phase 0 probe)

1. Endpoint `tagger.scryfall.com/graphql` exists and accepts POST with GraphQL body.
2. Schema exposes: tag list, tag hierarchy, per-tag cards, tag metadata.
3. Rate limiting is reasonable (< 1 req/sec is safe).
4. Response includes `oracle_id` for function tags and printing `id` for art tags.
5. Tag hierarchy is traversable (parent → children links).

If any of these fail, fallback is **Scryfall search-API-only** pull: enumerate known tag names from a community-maintained list, query `otag:name` for each. Less comprehensive (no hierarchy or descriptions) but works without GraphQL.

### Pull procedure (once probe passes)

1. **Tag catalog query** — GraphQL → full tag list with hierarchy → write `tag_catalog` table. Each row includes `card_count_expected` from Tagger's own count.
2. **Leaf-tag enumeration** — identify tags with no children (leaves). Parents get auto-populated via hierarchy rollup.
3. **Per-tag card pull** — for each leaf `otag`, paginate Scryfall search API `otag:<name>`. Store `(oracle_id, tag_name, source="scryfall_search")` rows. Rate limit 75ms.
4. **Hierarchy rollup** — walk tree; for each parent, union child tag cards. Store as `source="hierarchy"` rows (distinguishable from direct pulls).
5. **Art tag pull** — same procedure with `atag:`, keyed on printing_id.
6. **Completeness check** — per tag, compare `actual` count in local DB vs `card_count_expected` from catalog. Write `coverage_reports`. Any diff > 1% flags as incomplete; UI surfaces "re-fetch missing" button.
7. **Spot-check** — pick 50 random owned cards, query their Tagger page individually, confirm tag list matches local cache. Catches cards that exist in Tagger but never surfaced in any tag query.

### Incremental refresh

- Weekly refresh does step 1 first; if the tag catalog is unchanged, only re-pull tags whose `card_count_expected` differs from local.
- Full rebuild option (for when Tagger tree changes shape).

### Failure modes

- **GraphQL endpoint down or shape changed** → probe fails → refresh aborts, UI shows stale-data warning, sort still works (cached tags available).
- **Rate limit trip** → exponential backoff, resume from last completed tag.
- **Tag renamed** → catalog diff detects, migration step renames rows.
- **Tag removed** → remove local rows (after confirmation prompt, in case it's a transient error).
- **New card not yet tagged** → covered by weekly refresh; force-refresh button for post-release weeks.

---

## Verification plan (applies to every source, not just Tagger)

Every external data source gets four artifacts:

### 1. Probe script

Single file `probes/probe_<source>.py`:
- Hits endpoint with minimal known-good request.
- Asserts response is JSON/GraphQL/etc. with expected top-level keys.
- Writes response to `tests/probe_snapshots/<source>_<timestamp>.json`.
- Diffs against pinned `tests/probe_snapshots/<source>_pinned.json`.
- Exit 0 if shape matches, non-zero if drifted.

Run manually, on dev, and as a pre-refresh hook.

### 2. Schema snapshot tests

`tests/enrichment/test_<source>_schema.py`:
- Loads the pinned snapshot.
- Validates with a Pydantic model (or dataclass + type checks).
- Fails if the data model drifts.
- Re-pin explicitly via a script, so drift is always intentional.

### 3. Completeness verification

For each source, a different check:

| Source | Completeness check |
|---|---|
| Scryfall bulk | Compare card count vs Scryfall's published total |
| Tagger | Per-tag card count vs Tagger's own `card_count_expected` |
| EDHREC staples | Sum of inclusion rates above threshold ≈ expected count range (50 ± 5% of cards over 5% inclusion in known large meta) |
| edhtop16 | Tournament count and deck count in window match edhtop16 stats page |
| Commander Spellbook | Combo count matches their published total |
| Moxfield | Deck card count matches `deck.mainboard_count` from response metadata |
| Buylists | N/A (no authoritative total); check day-over-day delta sanity (< 10% row count change) |

Results logged to `coverage_reports` table; Database tab shows a health indicator per source.

### 4. Integration tests (cross-source sanity)

`tests/enrichment/test_cross_source_sanity.py`:
- **Known-staple test**: Sol Ring should be tagged `staple:universal`, have EDHREC inclusion > 50%, be in ≥ 1 combo (Basalt Monolith pair), have non-zero buylist. If any fail, a source pipeline is broken.
- **Known-vanilla test**: Grizzly Bears should have `otag:vanilla`, zero combos, near-zero salt, staple tiers empty.
- **Known-combo test**: Thassa's Oracle should be flagged by Spellbook as part of the Consultation combo, tagged `staple:cedh`.
- **Known-cull test**: Squire (the vanilla 1W 1/2) should appear in dead-weight cull view given default settings.

These tests assert end-to-end pipeline correctness. They'll catch regressions even if individual probes pass.

### 5. Per-function unit tests

Each enrichment function (`pull_tagger_tags`, `pull_edhrec_staples`, `compute_staple_tier`, `push_moxfield_diff`, etc.) gets unit tests:
- **Happy path**: runs against a fixture response, produces expected DB rows.
- **Empty response**: handles gracefully, doesn't wipe existing data.
- **Malformed response**: raises with a clear error, doesn't partial-commit.
- **Idempotency**: running twice produces identical DB state.
- **Rate-limit simulation**: mock 429 responses, verify backoff.

Fixtures live in `tests/fixtures/<source>/`; kept small, checked in.

### 6. Ongoing monitoring

- After each scheduled refresh, SocketIO emits a summary event: `{source, rows_changed, coverage_pct, duration_ms, errors}`.
- Database tab has a per-source health card: last-success timestamp, coverage %, last-error if any.
- Degrade gracefully: if a source is stale > N days, UI shows a banner on any feature depending on it.

---

## Tab consolidation plan (final state)

| Tab | Contents |
|---|---|
| Dashboard | Hardware/camera status, activity log, quick stats, Database subsection (update/refresh controls per source), Calibration button → modal wizard |
| Bin Setup | Physical layout, overflow chains, saved configs, bin testing |
| Sort Session | Session start wizard (with unified preset UI), live card info overlay, staging ROI, session stats, undo |
| Collection | Inventory table, tag filters, wishlist, physical locator, dead-weight cull view, Moxfield sync controls, box/divider management |

Motion Preview: hidden. Calibration: modal launched from Dashboard. Database: Dashboard subsection.

---

## Open questions / deferred decisions

- **strictlybetter.eu** — defer to after full scan; revisit as layer 2 of dead-weight filter.
- **MTGStocks** — low priority unless Goldfish RSS proves insufficient.
- **Archidekt** — deferred entirely (user doesn't use it).
- **Dominated-by heuristic** — explicitly punted in favor of strictlybetter.eu when that lands.
- **Authentication** — still deferred; revisit if exposing server beyond LAN.
- **Webhook for wishlist hits** — which service? Discord/ntfy both easy; decide when building phase 4.
- **Sync targets beyond Moxfield** — Moxfield is phase 3; other platforms optional later.

---

## Suggested build order (reconfirm before implementation)

1. Phase 0 items (unified preset UI, enrichment.db, scheduler, probes, Motion Preview hide)
2. Scryfall Tagger cache (Phase 1 item 6) — foundation for a lot of later filters
3. Physical locator + divider system (Phase 2 items 11-12) — immediate daily-use value
4. EDHREC + edhtop16 staples (Phase 1 items 7-8)
5. Tag filters in Collection (Phase 2 item 13)
6. Dead-weight cull view (Phase 2 item 14) — now all its inputs exist
7. Moxfield pull/push + buylists (Phase 3)
8. UI polish (Phase 4)

Each phase's deliverables are independently useful; the project can ship incrementally.
