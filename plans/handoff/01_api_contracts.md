# API Contracts

Per-feature specs for new HTTP endpoints and SocketIO events. All endpoints follow existing conventions from `web_server.py`:

- Route prefix: `/api/`
- Framework: Flask + flask_socketio
- Response format: JSON via `jsonify(...)`; errors return `{'error': '<message>'}` with 4xx/5xx status
- Naming: lowercase-snake-case for both URL segments and SocketIO event names
- All emits go through `socketio.emit(event, data)`; long-running operations emit progress events

---

## Unified Sort Preset UI (Phase 0)

### `GET /api/sort/presets`
List available presets (built-in + user-saved).
**Response:**
```json
{
  "presets": [
    { "id": "color", "name": "By Color", "builtin": true, "bin_count": 6 },
    { "id": "price_tiers_v1", "name": "Price Tiers", "builtin": true, "bin_count": 4 },
    { "id": "user_cedh_staples", "name": "My cEDH Pull", "builtin": false, "bin_count": 8 }
  ]
}
```

### `GET /api/sort/presets/<preset_id>`
Return the expanded preset as editable queries (the shape the unified UI renders).
**Response:**
```json
{
  "id": "price_tiers_v1",
  "name": "Price Tiers",
  "builtin": true,
  "bins": [
    { "bin": 1, "query": "usd>=5", "description": "$5 and up" },
    { "bin": 2, "query": "usd>=1 usd<5", "description": "$1–$5" },
    { "bin": 3, "query": "usd<1", "description": "Bulk" }
  ],
  "fallback_bin": 3
}
```

### `POST /api/sort/presets`
Save a new preset (or `PUT` to overwrite existing user preset).
**Body:**
```json
{ "name": "My cEDH Pull", "bins": [...], "fallback_bin": 8 }
```
**Response:** `{ "id": "user_my_cedh_pull", "name": "..." }`

### `DELETE /api/sort/presets/<preset_id>`
Only for user-saved presets. Returns 403 for builtin.

**Notes:**
- Remove the existing "Load from file" button from Sort Options; its backing endpoint stays (used by the above internally via the same preset store).
- Legacy `sort_configs/*.txt` files are auto-imported as read-only presets on startup.

---

## Enrichment data access (Phase 1+)

### `GET /api/enrichment/card/<oracle_id>`
Return all enrichment data for one card (for the live card-info overlay and collection drill-down).
**Response:**
```json
{
  "oracle_id": "...",
  "name": "Sol Ring",
  "scryfall": { "mana_cost": "{1}", "type_line": "...", "oracle_text": "...", "prices": {...} },
  "tags": ["ramp", "artifact-ramp", "mana-rock"],
  "art_tags_by_printing": { "<printing_id>": ["cat", "sci-fi"] },
  "staples": { "universal": true, "cedh": true, "archetype_count": 42 },
  "salt": 2.4,
  "combos": [ { "combo_id": "...", "with": ["Basalt Monolith"], "result": "Infinite mana" } ],
  "buylists": [ { "vendor": "ck", "price_usd": 1.25, "updated_at": "2026-04-18T12:00:00Z" } ],
  "deck_usage": { "moxfield": [ { "deck_id": "...", "deck_name": "..." } ] },
  "archetypes": ["artifact", "group-slug"],
  "sync_status": { "scryfall": "fresh", "edhrec": "stale_3d", "tagger": "fresh" }
}
```

### `GET /api/enrichment/query?q=<scryfall-like>`
Run a query with enrichment extensions (`otag:`, `staple:cedh`, `salt>2`, `combo:true`, etc.) against the full corpus, not just owned cards.
**Response:**
```json
{ "total": 142, "page": 1, "per_page": 50, "cards": [ {oracle_id, name, enrichment summary}, ... ] }
```

### `GET /api/enrichment/sources`
Source health overview (for Database subsection of Dashboard).
**Response:**
```json
{
  "sources": [
    { "name": "scryfall_bulk", "last_success": "...", "coverage_pct": 100.0, "status": "ok" },
    { "name": "tagger", "last_success": "...", "coverage_pct": 98.7, "status": "degraded", "error": "3 tags mismatched" },
    { "name": "edhrec", "last_success": "...", "coverage_pct": 100.0, "status": "ok" },
    { "name": "edhtop16", "last_success": "...", "coverage_pct": 100.0, "status": "ok" },
    { "name": "spellbook", "last_success": "...", "coverage_pct": 100.0, "status": "ok" },
    { "name": "buylist_ck", "last_success": "...", "coverage_pct": 100.0, "status": "ok" }
  ]
}
```

### `POST /api/enrichment/refresh/<source>`
Trigger manual refresh. Async; progress via SocketIO.
**Response:** `{ "started": true, "source": "tagger" }`

**SocketIO events:**
- `enrichment_refresh_progress` — `{ source, step, progress, total, message }`
- `enrichment_refresh_complete` — `{ source, duration_ms, rows_changed, coverage_pct, errors: [] }`

### `POST /api/enrichment/probes/run`
Run all probe scripts, report shape drift.
**Response:** `{ "results": [ { "source": "tagger", "ok": true/false, "diff": [...] } ] }`

---

## Physical locator + divider/box tracking (Phase 2)

### `POST /api/locator/query`
Find owned cards matching a query, with physical locations.
**Body:** `{ "query": "otag:removal color:w", "group_by": "box_divider" }`
**Response:**
```json
{
  "total_cards": 14,
  "groups": [
    { "box_id": "box-3", "divider_id": 14, "count": 6, "cards": [ {name, qty, oracle_id}, ... ] },
    { "box_id": "box-3", "divider_id": 15, "count": 8, "cards": [...] }
  ]
}
```

### `GET /api/locator/boxes`
List all known boxes and their divider ranges.
**Response:** `{ "boxes": [ { "box_id": "box-3", "divider_min": 1, "divider_max": 42, "card_count": 612 } ] }`

### `POST /api/locator/boxes`
Create a new box.
**Body:** `{ "box_id": "box-4", "label": "Modern bulk", "starting_divider": 1 }`

### `POST /api/storage/session-plan`
Attach a storage plan to a sort session.
**Body:** `{ "session_id": 123, "box_id": "box-3", "starting_divider": 15 }`

### `POST /api/storage/locations/<scan_id>`
Manually edit a card's location (e.g., after you rearranged a bin).
**Body:** `{ "box_id": "box-3", "divider_id": 17, "confidence": "manually_edited", "notes": "moved during cube cut" }`

### `GET /api/storage/session/<session_id>/summary`
Printable summary of a completed session: bin → divider mapping, card lists per divider.
**Response:** renders to label.html template if `?format=html`, else JSON.

**SocketIO events:**
- `storage_divider_advanced` — `{ session_id, bin, box_id, divider_id }` — emitted when a bin fills during sort

---

## Moxfield integration (Phase 3, public-only)

### `POST /api/integrations/moxfield/deck/import`
Fetch a public deck and convert to a sort preset.
**Body:**
```json
{
  "deck_url": "https://moxfield.com/decks/abc123",
  "printing_mode": "prefer_listed",
  "target_bin_strategy": "single_bin"
}
```
- `printing_mode` ∈ `{"exact", "any", "prefer_listed"}` (default `any`)
- `target_bin_strategy` ∈ `{"single_bin", "by_category", "by_color"}`

**Response:** same shape as `GET /api/sort/presets/<id>` — returned preset is saved as a user preset with name `"Moxfield: <deck_name>"`.

### `POST /api/integrations/moxfield/binder/import`
Import a binder as a wishlist.
**Body:** `{ "binder_url": "...", "merge_with_existing": true }`
**Response:** `{ "imported": 234, "skipped_duplicates": 12 }`

### `GET /api/integrations/moxfield/deck/<deck_id>/cache`
Check cached copy; returns cached shape + `fetched_at` or 404.

**SocketIO events:**
- `moxfield_import_progress` — `{ step, progress, total, message }`

---

## Collection tab (Phase 2)

### `GET /api/collection/inventory?filters=...&sort=...&page=...`
Main inventory query with enrichment joins. `filters` is a JSON object supporting:
```json
{
  "query": "otag:removal",
  "tag_chips": ["ramp", "removal"],
  "staple_tier": ["universal", "cedh"],
  "salt_min": 0,
  "salt_max": 3,
  "price_min": 0.5,
  "price_max": null,
  "in_decks": "any|none|specific_deck_id",
  "has_buylist": true,
  "location_box": null
}
```
**Response:** paginated list with enrichment summary per card.

### `GET /api/collection/filters/available`
Returns the filter universe for the sidebar: all tags with counts, staple tier counts, salt distribution, price range.

### `GET /api/collection/cull-candidates?preset=default`
Dead-weight cull view. Preset defaults:
- `default`: vanilla + no-staple + no-buylist + not-in-decks
- `strict`: default AND price == 0
- `custom`: uses body filters

**Response:** same shape as inventory; includes `cull_reasons` per card.

### `POST /api/collection/cull/generate-sort`
Turn a cull-candidates list into a one-bin sort preset for the next physical pass.
**Body:** `{ "card_oracle_ids": [...], "bin_number": 1 }`
**Response:** `{ "preset_id": "user_cull_2026_04_18" }`

---

## Wishlist priority bin + notifications (Phase 4)

### `GET /api/wishlist` / `POST /api/wishlist`
Existing endpoints — augmented with `priority` field (bool).

### `POST /api/wishlist/notification-targets`
Configure notification webhooks.
**Body:** `{ "browser": true, "webhooks": [ { "type": "discord", "url": "..." }, { "type": "ntfy", "topic": "..." } ] }`

**SocketIO events:**
- `wishlist_match` — `{ scan_id, oracle_id, card_name, priority_bin: int }`

---

## Session gallery + analytics (Phase 4)

### `GET /api/sessions?filter=...&page=...`
List sessions with aggregate stats.

### `GET /api/sessions/<id>`
Session detail: all scans, stats, bin distribution, motion path, storage plan.

### `GET /api/sessions/<id>/scans?filter=...`
Filtered scan list within a session (for gallery).

### `GET /api/scans/<scan_id>/image?variant=<crop|frame|matched>`
Serve the stored image for a scan. (Requires scan image persistence; check whether current worker stores crops — if not, this is gated on adding that.)

---

## Global conventions

- **Errors**: `{'error': 'human-readable message', 'code': 'machine-readable-slug'}` with appropriate 4xx/5xx.
- **Pagination**: `?page=<int>&per_page=<int>`; response includes `total`, `page`, `per_page`, `has_more`.
- **Long operations**: return `202 Accepted` immediately with a job id; progress via SocketIO; completion event has the same job id.
- **SocketIO event shape**: always `{..., ts: <iso8601>}` so clients can ignore stale events.
- **CORS**: inherits from existing server config; no changes.
- **Auth**: none for v1. If added later, use a header-based token; do not change endpoint paths.
