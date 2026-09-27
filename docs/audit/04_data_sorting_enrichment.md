# Audit 04 — Data model, bin routing (sorting), enrichment

Scope: `collection_db.py`, `enrichment_db.py`, `sort_config.py`, `sorting.py`, `query_parser.py`,
`cards.py`, `seed_card_universe.py`, `web_enrichment/*`, `sort_configs/*.txt`, `backfill_*.py`,
`apply_*.py`, `correct_session_csv.py`, `list_stamp.py`, `printings_map.json` (structure only),
and plans `web_enrichment_plan.md`, `web_enrichment_source_probes.md`, `query_helper_plan.md`,
`bulk_edit_plan.md`, `legal_prep.md`, `docs/speculative_bin_commit_PLAN.md`.

Audit date: 2026-09-27, branch `docs/audit-and-backlog`. No code modified.

---

## 1. Data model

### 1.1 Scryfall bulk data (the in-memory card universe)

- `config.py:16` pins `CARDS_JSON_PATH` to one specific file, `default-cards-20260521210656.json` (~539 MB).
  There are **three** ~539 MB `default-cards-*.json` files in the repo root (1.6 GB total).
- `cards.py:9-14` runs `json.load` on the whole file **at import time**. It builds `CARDS_DATA` (a list) and
  `CARD_DATA_BY_ID` (a dict keyed by Scryfall printing `id`). The full Python object graph takes several GB of
  RAM and loading takes tens of seconds. Every process that imports `cards` (including `query_parser`,
  through `import cards` at `query_parser.py:45`) pays this cost.
- `printings_map.json` (47 MB, 59,273 keys) is keyed by a representative printing id. Each value is
  `{name, illustration_id, frame, printings:[{id,set,set_name,collector_number,lang,frame}]}`, which gives
  one group per illustration. It is used for art-group price minimums and for the "Sets" list.
- Built lazily from the bulk data:
  - `_name_price_index` (`cards.py:106`)
  - `_art_min_price_index` and `_cheapest_printing_index` (`cards.py:131`)
- Other scripts glob for the newest `default-cards-*.json` instead of using `config.CARDS_JSON_PATH`, and each
  one re-parses the full 539 MB file: `seed_card_universe.py:14`, `backfill_session_metadata.py:22`,
  `correct_session_csv.py:59`. They can silently read a different snapshot than the server does.

### 1.2 `collection.db` (collection_db.py)

WAL mode, `foreign_keys=ON`. Tables are created and migrated on **every** `get_connection()` call
(`collection_db.py:32-40`).

| Table | Key columns | Notes |
|---|---|---|
| `sessions` | id, start_time, end_time, sort_mode, config_name, bin_count, total/recognized/unrecognized, notes, config_text | `end_time IS NULL` means a stale session (power-loss resume, `:284`) |
| `scan_history` | id, session_id→sessions, scan_num, timestamp, name, set_code, collector_number, oracle_id, illustration_id, colors, cmc, type_line, rarity, price_usd, **bin**, method, hash_distance, recognized, is_foil, foil_confidence, frame, border_color, frame_effects | Append-only log. The only stored timestamp is the record time. |
| `inventory` | id, name, set_code, collector_number, oracle_id, …, quantity, foil_quantity, first/last_scanned, box (legacy text), divider_id | The logical key is (name, set_code, collector_number), but it has **no UNIQUE constraint**. There is only a non-unique index (`:105`). |
| `wishlist` | name, set_code, max_price, priority, found | Legacy name wishlist |
| `boxes`, `dividers` | boxes.name UNIQUE; dividers(box_id→boxes, label, position, capacity) | Phase 2.12 storage |
| `detection_reviews` | (scan_id→scan_history, variable) UNIQUE, detected_value, confidence, verdict, correction | Review queues |
| `moxfield_wishlists`, `moxfield_wishlist_cards` | source_key UNIQUE; cards(wishlist_id→, oracle_id, …) | Priority-bin cache |

Migrations (`:188-238`) are ad-hoc: `SELECT col` → `except OperationalError` → `ALTER`. There is no
`schema_version` / `PRAGMA user_version`.

**Name collision:** `enrichment.db` also has a table called `moxfield_wishlists`
(`enrichment_db.py:217`), with a completely different schema keyed on (username, oracle_id). Two parallel
Moxfield wishlist caches exist:

- `collection_db` version: the priority bin, `web_worker._check_priority_match`
- `enrichment_db` version: the `wishlist:` query token and the `deck_usage` table

### 1.3 `enrichment.db` (enrichment_db.py)

Keyed on oracle_id across the whole Scryfall corpus (`enrichment_db.py:42-245`).

- `tags` (oracle_id, tag_name) and `art_tags` (printing_id, tag_name), holding otag and atag membership
- `tag_catalog` (tag_name PK, tag_type, parent, description, card_count_expected, source, last_updated)
- `staples` (oracle_id, tier, source) with tier ∈ universal/archetype/cedh; `card_rankings` (oracle_id, source) → rank
- `salt_scores`, `themes`, `commander_ranks`
- `combos` and `combo_membership` (FK to combos)
- `buylists` (oracle_id, vendor) PK
- `price_history`, `local_tags` (unused by any source in scope)
- `sync_metadata` (per source), `coverage_reports` (append-only)
- `card_universe` (oracle_id → name), seeded by `seed_card_universe.py`
- `moxfield_decks` (raw_json blob), `moxfield_wishlists`, `deck_usage` (overlay rebuilt atomically)

Migrations use `_add_column_if_missing` (`:266`).

### 1.4 Session and scan recording

1. `web_worker.start_session` builds a `SortConfig` from inline lines or a preset file
   (`web_worker.py:1120-1172`). `collection_db.start_session` (`:247`) stores `config_name` and/or
   `config_text` so the session can be resumed.
2. For each card: identification, then `SortConfig.get_bin(routing_card_data)` (`web_worker.py:1857-1862`).
   `routing_card_data` is a shallow copy of the Scryfall dict plus `is_foil` and `printing_disambiguated`.
3. Post-routing overrides, in order:
   - identity-confidence gate → fallback bin (`web_worker.py:1878-1891`)
   - legacy wishlist bin (`:1893`)
   - Moxfield priority bin (`:1920`)
4. `scan_tracker.record_scan` → `collection_db.record_scan` (`collection_db.py:341`). This always appends to
   `scan_history`. Recognized cards are upserted into `inventory` (qty+1, and foil_qty+1 if the card is foil),
   then committed.
5. `end_session` (`:272`) writes the totals.
6. Resume: `find_stale_sessions` (`:284`) and `get_session_bin_counts` (`:306`) rebuild bin fill counts from
   `scan_history.bin`.

Per-session CSV logs (`scan_logs/session_*/scans.csv`) are a second source of truth. The `backfill_*`,
`apply_*` and `correct_session_csv.py` scripts patch that CSV. Most of them never touch the DB, so the two
sources drift apart.

### 1.5 How a card's bin is chosen

**Config file format** (`sort_config.py:249` `from_lines`):

- `bins: N`, `fallback: K`, optional `limit: M` (global per-bin cap), optional `overrides: a,b`
- then `binX: <query>` lines

Several lines for the same bin number form an overflow chain.

**Evaluation** (`sort_config.py:72` `get_bin`):

1. Override bins first, in the listed order, then regular bins in declaration order. The first match wins.
2. A bin at `bin_limit` is skipped, so the card overflows to the next match.
3. If nothing matches, the card goes to the fallback bin.
4. **Side effect: `bin_card_counts[bin] += 1` happens inside `get_bin`.**
5. If any query uses an enrichment token, `_get_enrichment_data` (`:133`) looks up
   staple/salt/combo/buylist/rank once per oracle_id and caches the result for the session.
6. If any query uses `otag:`, `fetch_otag_data` (`:528`) pulls every tag's oracle_ids from the **live
   Scryfall search API** when the config is parsed. That happens at session start.

**Query grammar** (`query_parser.py:313`):

```
expression := or_expr
or_expr    := and_expr ('or' and_expr)*
and_expr   := not_expr (not_expr)*          # implicit AND
not_expr   := '-' atom | 'not' atom | atom
atom       := '(' expression ')' | FIELD_QUERY
FIELD_QUERY:= field (':'|'='|'<'|'>'|'<='|'>='|'!=') value  |  bare-word (→ name:)
```

Fields (`FIELD_ALIASES`, `:110`):

| Group | Fields |
|---|---|
| Card attributes | c, ci/id, t, cmc/mv, s/e/set, r, usd/price, pow, tou, o, name, is:*, kw, otag, legal/f, produces, st |
| Enrichment | staple, salt, combo, buylist:ck[op N], cull, deck:<id>[!exact], wishlist:<user>[!exact], edhrec-top, edhtop16-top/cedh-top |

Evaluation (`evaluate_query`, `:1030`) is a recursive walk with short-circuiting `all`/`any`.

Price semantics (`:593-635`):

1. A foil scan uses `usd_foil`.
2. A disambiguated printing uses its own `usd`.
3. Otherwise the art-group minimum nonfoil price (`cards.get_art_min_price`) is used, falling back to the
   card's own `usd`.

**Cost per card.**

- Plain field queries take microseconds (dict lookups and string `in`).
- Enrichment tokens, first sighting of each oracle_id: opens a new SQLite connection (the full CREATE/migrate
  script runs every time) plus 6 SELECTs, roughly 5–20 ms.
- `deck:` token, **every card, every query**: opens the DB, reads and `json.loads` the whole deck raw_json,
  re-runs `parse_deck_cards` (with a basic-land lookup per slot), then scans linearly. That is tens of ms per
  card per `deck:` clause (`web_enrichment/moxfield.py:673-709`).
- `wishlist:` token: opens a DB per evaluation (`moxfield.py:712`).
- The legacy wishlist override opens `collection.db` per card (`web_worker.py:1896`).

---

## 2. Per-file summary

### collection_db.py (1887 lines)

- Schema and migrations: `_create_tables` `:43`
- Sessions: `start_session` `:247`, `end_session` `:272`, `find_stale_sessions` `:284`,
  `get_session_bin_counts` `:306`, `get_session_metadata` `:322`
- Scans: `record_scan` `:341`, `get_scan_history` `:622`, `get_unrecognized_scans` `:637`,
  `resolve_unrecognized_scan` `:676`
- Inventory: `get_inventory` `:483`, `search_collection` `:499`, CSV export/import `:786/:802`,
  `assign_box` `:907`, `add_inventory_item` `:1337`, `delete_inventory_item` `:1374`
- Boxes/dividers: `:965-1159`
- Locator: `locate_cards_by_query` `:1214`, which runs the query parser over the whole inventory and does a
  `card_lookup` per row
- Legacy wishlist `:1423-1462`; Moxfield wishlist cache `:1476-1552`
- Detection reviews: `upsert_detection_review` `:1566`, `list_detection_reviews` `:1605`,
  `seed_detection_reviews_from_scans` `:1714`
- `get_cull_candidates` `:578` delegates to `web_enrichment.cull`

### enrichment_db.py (469 lines)

- `get_connection` `:28` (creates and migrates every call)
- `_create_tables` `:40`
- `backup_db` `:281`
- `seed_card_universe` `:295`
- `record_sync_attempt` `:321` (atomic upsert)
- `record_coverage` `:365`
- `rebuild_deck_usage` `:388`

### sort_config.py (595 lines)

- `SortConfig.__init__` `:31` precomputes the override/regular split
- `get_bin` `:72`
- `_get_enrichment_data` `:133`
- `from_lines` `:249`, `from_file` `:396`, `to_lines` `:409`
- `prompt_manual_config` `:432` (CLI, duplicates the otag and enrichment wiring from `from_lines`)
- `fetch_otag_data` `:528`: `requests` with no timeout and no User-Agent

### sorting.py (62 lines)

- Global `_active_sort_config` with setter and getter (`:21`, `:27`)
- `draw_info_as_json` `:55`
- The legacy `get_bin_number` was removed, but `test_10_cards.py:29` and `test_full_sort.py:36` still import
  it, so both fail with ImportError.

### query_parser.py (1124 lines)

- `tokenize` `:193`
- `Parser` `:313`
- `collect_otag_terms` `:411`, `collect_enrichment_fields` `:428`
- `_eval_color_field` `:490`, `_eval_field_query` `:542`, `_eval_is_predicate` `:959`
- `evaluate_query` `:1030`
- `expand_otag_cache` `:1075` (parent rollup)

### cards.py (365 lines)

- Import-time load `:9-26`
- `reload_card_data` `:28`
- Price indexes `:106`, `:131`
- `get_art_min_price` `:175`, `get_cheapest_printing_id` `:186`, `_get_price_str` `:210`
- `extract_card_info` `:256`
- `card_is_allowed` `:313`
- `get_same_illustration_english_candidates` `:350` (linear scan over every card)

### seed_card_universe.py (57 lines)

Loads the newest bulk file and upserts (oracle_id, name) for English printings into `card_universe`.

### web_enrichment/

- **`base.py`**: the `EnrichmentSource` ABC (probe, refresh, coverage_report) and `RefreshResult`.
- **`repo.py`**: `EnrichmentRepo`, a read layer that opens a new connection per call.
  - `get_card` `:53`
  - `is_cull_candidate` `:424`
  - `query()` `:501` is still a Phase 0A stub that returns `[]`
- **`scheduler.py`**: APScheduler cron wrapper.
  - `register` `:81`
  - `trigger` `:129` (manual trigger in a thread)
  - `_run_source` `:172` (overlap guard)
- **`stubs.py`**: placeholder sources.
- **`vanilla.py`**: `is_vanilla_or_french_vanilla` `:44`.
- **`tagger.py`**: `TaggerSource` uses Scryfall search `otag:` / `atag:` at 1 request/second.
  - `_fetch_tag` `:365`, `_fetch_atag` `:420`, `_write` `:484`
  - Incremental skip `:227`
  - The tag list comes from `tag_catalog` (`:186`)
- **`scrape_tagger_catalogue.py`**: scrapes the HTML of `scryfall.com/docs/tagger-tags` with a regex
  (`:51-54`) and upserts roughly 5k otags and 11k atags into `tag_catalog` (`:239`).
- **`scrape_tagger_descriptions.py`**: a Playwright script that logs into Scryfall and scrapes Tagger's SPA
  behind its auth wall.
- **`edhrec.py`**: `EDHRECSource` pulls from `json.edhrec.com`:
  - top/year, 32 color pages, up to 350 theme pages, top-commanders, and one detail page per card (thousands)
  - rate: 1 request/second
  - `_compute_enrichment` `:425`, `_compute_rankings` `:665`
  - `_write` `:592` and `_write_rankings` `:724` run as separate transactions
- **`edhtop16.py`**: GraphQL for 6 months of top-16 decklists, capped at 500 tournaments (`:241`), with an
  annual-staples fallback (`:354`), `_write` `:373` and rankings `:391`.
- **`spellbook.py`**: pages through `/variants/` 100 at a time (`_fetch_all` `:160`) and upserts
  (`_write` `:239`).
- **`buylist_ck.py`**: CK's undocumented `api/json` with an HTML fallback. It resolves names to oracle_id
  through `cards.CARD_DATA_BY_ID` (`:487`) and does an atomic delete+insert (`:401`).
- **`cull.py`**: `ATTACH enr` plus a join that scores `keep_confidence` (`get_cull_candidates` `:74`).
- **`moxfield.py`**: undocumented `api2.moxfield.com/v2` deck and wishlist fetch, parse and cache.
  - `is_in_deck` `:673`, `is_in_wishlist` `:712`
  - `MoxfieldSource.import_deck` `:852`, `import_wishlist` `:911`
- **`moxfield_text.py`**: plain-text decklist parse, resolve and render. `resolve_entries` `:87` rebuilds a
  name index over every card on each call.
- **`scryfall_query_translator.py`**: turns the DSL into a Scryfall URL, dropping enrichment tokens
  (`to_scryfall` `:85`).

### Scripts

- **`backfill_csv_from_db.py`**: copies `manual_review` names from the DB into a session CSV. The default
  session id (36) and path are hardcoded (`:95-97`).
- **`backfill_session_metadata.py`**: adds frame/border/frame_effects to an old `scans.csv` from the bulk
  JSON.
- **`apply_foil_verdicts.py`**: applies the review-page foil verdicts to `scans.csv`. CSV only.
- **`apply_verified_corrections.py`**: a one-off hardcoded session/scan fix-up that writes to the DB and the
  CSV.
- **`correct_session_csv.py`**: re-identification and foil check with Claude Haiku vision (`MODEL` `:45`).
  CSV only.
- **`list_stamp.py`**: detects The List planeswalker stamp by template match. It is not on the main path.

### sort_configs/

13 presets. Built-ins: color, mana_value, set, price, type. Custom ones: drej, money legends, over-under,
pull valuables, under-over, edh_staples, color_type, price_tiers.

---

## 3. Findings

### 3.1 Bugs (functional)

| # | Sev | Location | Finding |
|---|---|---|---|
| B1 | **High** | `web_enrichment/buylist_ck.py:155` + `:416` | The PK is `(oracle_id, vendor)`, but one row is appended per CK product (per printing/set) with no de-duplication. The plain `INSERT` hits a UNIQUE violation as soon as two printings share an oracle_id, which rolls back the whole refresh. **CK buylist data can never be written.** Fix: aggregate per oracle_id (max, or the nonfoil price) before inserting. |
| B2 | **High** | `buylist_ck.py:327-329` | On HTTP 429 the loop does `sleep(2); continue` with no retry cap. A sustained 429 makes it spin forever and hold a scheduler slot. |
| B3 | **High** | `web_worker.py:1898-1900` vs `collection_db.py:1456` | `check_wishlist_match(conn, card_name, set_code=...)`: the function takes no `set_code` argument. That raises TypeError on every card, which is caught and logged, so **the legacy wishlist bin never fires**. |
| B4 | **High** | `query_parser.py:551,555,653,639,645`, `vanilla.py:44` | Double-faced, transform, MDFC and adventure cards have no top-level `colors`, `oracle_text`, `power` or `toughness` in Scryfall; those fields live under `card_faces`. As a result: `c=w` / `c:w` fail and the card goes to the **fallback bin in color.txt**; `o:` misses; `cull:true` treats every DFC as **vanilla** (empty text), which is a false cull; pow/tou compares miss. The evaluator needs a face-merged view. |
| B5 | High | `test_10_cards.py:29`, `test_full_sort.py:36` | They import `sorting.get_bin_number`, which was removed on 2026-04-22. Both scripts are broken. |
| B6 | Med | `query_parser.py:558-560, 652-654, 679-682, 706-709` | `t`, `o`, `kw` and `produces` ignore the operator, so `t!=land` evaluates as `t:land`. Only `-t:land` negates. |
| B7 | Med | `query_parser.py:508-509` | `c<=wu` / `c>=rg` do a numeric compare against "wu", which returns False. Scryfall means subset/superset. Presets written from Scryfall habits silently mis-route. |
| B8 | Med | `query_parser.py:846-899` + `sort_config.py:185-197` | `cull` predicate 4 reads `enrichment_data["in_deck"]`, but `_get_enrichment_data` never fills it, so deck/wishlist membership is ignored by `cull:true` in bin queries. |
| B9 | Med | `web_enrichment/moxfield.py:561-603` | The wishlist cache PK is `(username, oracle_id)`. A wishlist holding two printings of the same card raises IntegrityError and the import fails. |
| B10 | Med | `scrape_tagger_catalogue.py:150-169` + `tagger.py:278-286` | `tag_catalog.tag_name` is the PK shared by otags and atags. The catalogue stores atags as raw slugs, so an otag and an atag with the same slug overwrite each other's `tag_type`. The old tagger path stored atags as `atag:<slug>`, so both conventions now exist side by side. |
| B11 | Med | `collection_db.py:907-950` `assign_box` | A split row drops `foil_quantity` and `divider_id`, and creates a second `(name,set,cn)` row. After that, `record_scan`'s `LIMIT 1` lookup (`:420`) increments an arbitrary one of the two rows. |
| B12 | Med | `collection_db.py:380, 714` | `price_usd = usd or usd_foil`, regardless of whether the scan was foil, so inventory value is wrong for foils. The routing price logic (`query_parser.py:611`) handles foil correctly, so the two disagree. |
| B13 | Med | `apply_verified_corrections.py:77,85,81,116` | Reads `info['set_code']` and `info['price_usd']`, which are not Scryfall keys (the correct key is `set`). It writes an **empty set_code**, price 0, and comma-joined colors, where `record_scan` uses `''.join`. Inventory is not updated. |
| B14 | Med | `tagger.py:227-238, 494-499`; `edhrec.py:608-658`; `edhtop16.py:373-386`; `spellbook.py:239-257` | Every source except buylists and rankings is **upsert-only**. Cards that fall out of a tag, staple tier, theme or combo stay in the table forever, and `staple:any` drifts toward true over time. |
| B15 | Med | `edhtop16.py:293-302`, `tagger.py:389-403`, `edhrec.py:290-300` | A mid-pagination error does `break`, and the partial dataset is written with `success=True`. For edhtop16 this changes the rate denominator. For EDHREC, a rate-limited run writes fewer pages and **replaces** `card_rankings` with a partial list (`:733`). |
| B16 | Low | `query_parser.py:1101-1106` | `_descendants` recurses with no cycle guard, so a cyclic `tag_catalog.parent` means RecursionError. In practice nothing populates `parent`, so the whole rollup is dead code. |
| B17 | Low | `enrichment_db.py:428` | `slot.get("card") or {} if isinstance(slot, dict) else {}` has a precedence bug: a non-dict slot raises AttributeError. |
| B18 | Low | `collection_db.py:832` | `int(row.get('quantity'))` in the CSV import is unguarded, so one bad row aborts the import after a partial transaction. |
| B19 | Low | `collection_db.py:676-783` | `resolve_unrecognized_scan` never sets `bin` or `foil_quantity`, and ignores foil. |
| B20 | Low | `scrape_tagger_descriptions.py:33,53,176,453` vs `:493` | The docstring and messages say `--auth` / `--run`, but argparse defines the subcommands `auth` / `run`. The generic selector `main p:first-of-type` (`:131`) can store the wrong text as a description. |
| B21 | Low | `sort_configs/over-under 1usd plus separate ramp with otag.txt`, `under over a dollar….txt`, `pull valuables and ramp and staples.txt` | These presets have **unreachable bins**: an earlier catch-all shadows bin5 or bin4. No lint catches this. |
| B22 | Low | `correct_session_csv.py:301` | Rewrites with fixed `CSV_COLS` and `extrasaction="ignore"`, which drops any extra columns. The name-match check is a lenient substring test. It only changes the name/set/cn in the CSV, not oracle_id or the DB. |
| B23 | Low | `cards.py:291` | `PRINTINGS_MAP.get(card_id)` only hits representative ids. Non-representative printings get `Sets=[own set]`. |
| B24 | Low | `cards.py:28-72` `reload_card_data` | Runs `CARDS_DATA.clear()` then `extend`. A sort thread reading during the reload sees an empty universe, and for a moment there are two copies in memory. |

### 3.2 Schema and migration risks

- There is no `user_version` or schema version in either DB, and migrations detect missing columns by
  catching exceptions (`collection_db.py:188-238`). `bulk_edit_plan.md` (provenance, `inventory_history`)
  and the speculative-commit instrumentation both need a versioned migration runner.
- `inventory` has no UNIQUE constraint on `(name, set_code, collector_number)`. Two web and worker
  connections doing SELECT-then-INSERT (`record_scan` `:420-452`) can create duplicate rows. The key also
  lacks lang and oracle_id.
- `scan_history` has no inventory_id link. `delete_session` (`:1396`) removes scans but leaves the inventory
  quantities they created, so the two tables drift.
- `get_connection` runs `executescript` DDL on every open (both DBs). It is also called on the per-card path
  (enrichment, deck, wishlist and wishlist-override lookups).
- There are two different `moxfield_wishlists` tables, one per DB (§1.2).
- `detection_reviews` cascade relies on `PRAGMA foreign_keys=ON`. Any script that opens the DB without
  `get_connection` (for example raw `sqlite3.connect` in tooling) leaves orphans.
- `enrichment_db` backups are manual only (`backup_db` `:281`). Nothing calls it before refreshes.

### 3.3 Scraping fragility, legal and ToS

`plans/legal_prep.md` already says the following. Code-level confirmation:

| Source | Mechanism | Fragility | Legal/ToS |
|---|---|---|---|
| Scryfall search (`tagger.py`, `sort_config.fetch_otag_data`) | Official API | Low. `fetch_otag_data` has **no timeout and no UA/Accept header** (`sort_config.py:552`); Scryfall requires both. Full tagger run: ~5k otags + ~11k atags at 1 request/second, **4–5+ hours**. | OK under the API terms. Requests carry a placeholder UA `example.invalid` (all sources, e.g. `tagger.py:56`). |
| Scryfall docs HTML (`scrape_tagger_catalogue.py`) | Regex over the HTML | Breaks on markup changes (guarded by a zero-result check) | Grey area |
| **Tagger auth-wall scrape** (`scrape_tagger_descriptions.py`) | Playwright with the user's session cookie | Very high (selectors are guesses) | **Explicitly circumvents an auth/CSRF wall** (its own docstring says "skirts what Tagger's auth wall is designed to prevent"). Remove it, or keep it strictly personal and never ship it. |
| EDHREC (`edhrec.py`) | Undocumented `json.edhrec.com`, thousands of requests per run | Medium. 403 is swallowed as `None` (`:294`) and the run still counts as success. | ToS says personal, non-commercial use. Not shippable in a paid product. |
| edhtop16 | Public GraphQL | Medium (schema drift) | Probably OK. Confirm with them. |
| Commander Spellbook | Public REST | Low. A bulk `variants.json` export exists and would be one request instead of hundreds of pages. | OK |
| **Card Kingdom** (`buylist_ck.py`) | Undocumented `api/json` plus HTML scrape | High, and currently broken by B1 | **Highest legal risk**: no license path (legal_prep §CK) |
| **Moxfield** (`moxfield.py`) | Undocumented `api2` behind Cloudflare | High (`moxfield_text.py:3-4` notes WAF blocks) | Moxfield ToS forbids scraping without permission. `username` is not URL-encoded (`moxfield.py:246`). |

Recommendation: add a per-source `enabled` / `shippable` flag and a legal-status field, and default the
risky sources (CK, Moxfield api2, EDHREC, Tagger scraper) off in any distributed build. Change the UA to a
real contact.

### 3.4 Performance

1. **Giant JSON at import.** `cards.py:9-14` puts 539 MB into multi-GB of dicts, and every script that
   touches it re-parses its own copy.
   - Fix: at download time, derive a slim artifact holding only the fields routing and ID need (id,
     oracle_id, name, set, cn, colors (face-merged), color_identity, cmc, type_line, oracle_text
     (face-merged), keywords, legalities, prices, rarity, set_type, flags, illustration_id, games, lang,
     frame fields).
   - Store it as SQLite with indexes, or as msgpack/pickle. Expected gain: about 10× less RAM and
     sub-second start.
   - Delete the redundant bulk snapshots (1.1 GB).
2. **Per-card enrichment.** Each call opens a fresh connection and runs DDL (`sort_config.py:154`). Better:
   when a session starts and the config needs enrichment, load a flat dict `oracle_id → {…}` for all
   ~30k oracle_ids in a single join. That is about 1–2 MB, gives O(1) lookups, and keeps SQLite off the
   routing path.
3. **`deck:` token** (`moxfield.py:673`): full JSON parse per card per clause. Precompute one set per deck
   (oracle_ids plus (set, cn)) once per session.
4. **`fetch_otag_data`** hits the network at session start, even though the same data sits locally in
   `enrichment.tags`. It should read the local table first (the scheduled tagger refresh keeps it current)
   and only fall back to the API for uncached tags. That removes the network dependency and the start-up
   latency.
5. `locate_cards_by_query` (`collection_db.py:1214`) makes one `card_lookup` call per inventory row, which is
   O(inventory) and fine up to about 40k rows. Pre-filter with SQL where possible.
6. `get_same_illustration_english_candidates` (`cards.py:350`) scans 100k+ cards linearly per call. Index it
   by illustration_id.
7. `moxfield_text.resolve_entries` (`:98-107`) rebuilds the name index on every call. Cache it.
8. `EnrichmentRepo` opens and closes a connection (with DDL) per method call. Reuse a connection or a
   thread-local one.

### 3.5 Other observations

- `repo.query()` has been a stub since Phase 0A (`repo.py:501`).
- Coverage-percentage formulas are meaningless or can exceed 100 (`tagger.py:301`, `edhrec.py:219`,
  `scrape_tagger_catalogue.py:212` passes a row count as `coverage_pct`).
- `scryfall_query_translator` reports `edhrec_top` / `edhtop16_top` as "unknown field" rather than
  enrichment-only. Dropping a clause inside an OR widens the query; the tooltip says so, but it is still
  approximate.
- `cull.py` wraps everything in a bare `except Exception: return []` (`cull.py:371`), so schema errors look
  like "no candidates".

---

## 4. Optimizations and consolidation

1. **One card-data service.** Replace `cards.CARDS_DATA` scans, `buylist_ck._build_name_index`,
   `moxfield._build_oracle_type_index`, `moxfield_text.resolve_entries`, `card_universe` and the script-local
   `load_scryfall_index` copies with a single indexed module backed by the slim artifact. It would expose
   by_id, by_oracle, by_name, by_(set, cn), by_illustration, plus face-merged fields.
2. **One Moxfield cache.** Merge the `collection_db.moxfield_wishlist*` tables with the
   `enrichment_db.moxfield_wishlists` and `deck_usage` tables so the priority bin, `wishlist:` and `cull`
   read the same data.
3. **Pure routing function.** `SortConfig.evaluate(card, ctx) -> bin` with no side effects, plus
   `commit(bin)` that increments counts. See §5.
4. **`RoutingContext`** built once per session, holding otag sets (from the local DB), enrichment
   `oracle_id → dict`, deck/wishlist sets, and price indexes. Every token then evaluates in memory.
5. **Config lint** at `from_lines`, which the UI surfaces:
   - unreachable bins (by sampling the card universe: a bin with zero matches after earlier bins)
   - unused bins
   - operator-ignored fields
   - enrichment tokens used while their source is empty or stale
6. **Enrichment sources: shared plumbing.** `_emit_progress`, `_record`, `_ms` and the httpx client with
   retry/backoff are copy-pasted in 8 modules. Add a `base.HttpSourceMixin` with a capped
   exponential-backoff GET/POST, a partial-failure flag, and a "replace-by-source" write helper. That also
   fixes B14 and B15 uniformly.
7. **Spellbook:** use the bulk export instead of about 300 page requests.
8. **Scripts:** fold `apply_foil_verdicts`, `backfill_csv_from_db` and `apply_verified_corrections` into one
   `session_fixup.py` that updates the **DB first** and regenerates the CSV from the DB, so there is one
   source of truth. Archive the hardcoded one-offs.
9. Use `PRAGMA user_version` and an ordered migration list for both DBs, and stop running DDL on every
   connect.

---

## 5. Impact of the planned motion redesign (speculative bin commit)

`docs/speculative_bin_commit_PLAN.md` gates this work on cycle time under 4 s. The data and sorting layer
still needs these changes before any early-bin logic can work:

1. **`get_bin` mutates state** (`sort_config.py:110-112, 130`). If the bin is evaluated speculatively (early
   guess, hedge, re-evaluate after ID), overflow counts get **double-counted**, the `limit:` overflow goes
   wrong, and the "first 5 fallback" log breaks. It needs a pure `predict_bin()` / `evaluate()` plus an
   explicit `commit_bin()` called when the card is physically released. Resume (`get_session_bin_counts`)
   already counts committed bins from `scan_history`, which is consistent with that split.
2. **Inputs that finalize late change the bin.** Routing depends on inputs that arrive after the first hash
   match:
   - `is_foil` (price field `query_parser.py:611`)
   - `printing_disambiguated` (`:619`)
   - the identity-confidence gate (it forces the fallback bin)
   - the wishlist and priority overrides (`web_worker.py:1878-1925`)

   Oracle-level predicates (color, type, cmc, otag, staple, salt, combo) are **stable across printings**;
   price and `set` are not. A useful primitive is `bin_candidates(oracle_id, candidate_printings)`: evaluate
   the config over every candidate printing × {foil, nonfoil}. If the result is a single bin, commit early
   with no risk. If it is several bins, hedge between them. This beats the statistical prior in the plan
   (§2) and answers its open question #4.
3. **Routing latency must be deterministic.** An early decision cannot wait on SQLite opens, DDL,
   deck-JSON parsing, or wishlist DB opens on the hot path (§3.4 items 2–3). Build the `RoutingContext` at
   session start.
4. **Bin probability model data.** Rarity counts per set come from the bulk JSON. Empirical per-session bin
   frequencies can come from `get_session_bin_counts`. A per-bin card count over the card universe (the
   predictor's cold-start prior) needs the pure evaluator run over the slim card table, which is the same
   machinery as the "unreachable bin" lint.
5. **Instrumentation (plan Phase 1) needs schema.** `scan_history` stores a single `timestamp`. Add
   `capture_ts`, `id_complete_ts`, `bin_decided_ts`, `move_start_ts`, `move_end_ts`, `provisional_bin`,
   `final_bin` and `speculation_outcome`, through a versioned migration (see §3.2).
6. **Doc drift.** The plan refers to `bin_configs/*.json`, but routing configs are `sort_configs/*.txt`. The
   bin **positions** come from `gcode_control.get_bin_locations()`. Fix the plan text.
7. **Fallback / reject bin** must be a first-class bin in the model. Right now it is also where low-confidence
   ID and unparseable enrichment land, so it is over-represented, which skews the predictor.

---

## 6. TODOs found

A grep for `TODO|FIXME|XXX|HACK` over the scoped code finds **no TODO markers**. The only hits are the
`card_XXXX.jpg` placeholders in `correct_session_csv.py:6,10`.

Implicit TODOs and stubs:

- `web_enrichment/repo.py:501`: `query()` is a Phase 0A stub.
- `web_enrichment/moxfield.py:779-815`: `refresh()` is a no-op (item "3.18").
- `web_enrichment/stubs.py`: all placeholder sources (they should be removed once the real ones are
  registered).
- `moxfield.py:50-53`: the wishlist response shape is "TBD until live probe confirms".
- `scrape_tagger_descriptions.py:446-458`: `DESCRIPTION_SELECTORS` are "educated guesses".
- `enrichment_db.py:69-74`: orphan `otag_relations` / `cluster_id` from the removed Otag Explorer.
- `sort_configs/set.txt`: a template with commented-out bins.
- Plans not started: `query_helper_plan.md` (status "not started, high priority") and `bulk_edit_plan.md`
  (not started; needs the provenance schema).
