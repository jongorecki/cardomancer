# Handoff: Web Enrichment & Integrations

Everything an implementation agent needs to build the plan in `../web_enrichment_plan.md`.

---

## Read in this order

1. **[../web_enrichment_plan.md](../web_enrichment_plan.md)** — overall scope and phasing
2. **[../web_enrichment_source_probes.md](../web_enrichment_source_probes.md)** — how each external source behaves
3. **[01_api_contracts.md](01_api_contracts.md)** — endpoints + SocketIO events per feature
4. **[02_ui_wireframes.md](02_ui_wireframes.md)** — ASCII sketches of every new view
5. **[03_acceptance_criteria.md](03_acceptance_criteria.md)** — "done looks like" per feature
6. **[04_scope_fences.md](04_scope_fences.md)** — what NOT to touch
7. **[05_dependency_pins.md](05_dependency_pins.md)** — libraries and versions
8. **[06_agent_parallelization.md](06_agent_parallelization.md)** — phase DAG and branch strategy
9. **[07_shared_interfaces.md](07_shared_interfaces.md)** — EnrichmentSource, EnrichmentRepo, schemas
10. **[08_secrets_handling.md](08_secrets_handling.md)** — `.env` + python-dotenv
11. **[09_backup_discipline.md](09_backup_discipline.md)** — git init + backup/ snapshots
12. **[10_code_style.md](10_code_style.md)** — Python/JS/CSS conventions to match
13. **[11_test_framework.md](11_test_framework.md)** — pytest layout and fixtures

---

## Quick-start for an implementation agent

1. Read the docs above in order.
2. Start with **Phase 0A** (see `06_agent_parallelization.md`). Do not skip.
3. Create a feature branch (naming in `06`).
4. Implement against the API contracts in `01` and the interfaces in `07`.
5. Match acceptance criteria in `03` before requesting merge.
6. Snapshot existing files to `backup/` before editing (per `09`).
7. Run `pytest tests/` and `python probes/run_all.py` before declaring done.

---

## Decisions already made (do not re-litigate)

- Stack: Flask + flask_socketio + SQLite + vanilla JS + Bootstrap 5 (unchanged)
- Secrets: `.env` file + python-dotenv
- Backup: git init + `backup/` snapshots (both)
- Moxfield v1: public-only (no auth)
- Collection tab layout: hybrid (filter sidebar + Inventory/Locator/Sync sub-views)
- Tab consolidation: done in Phase 0A (7 → 5 tabs; Calibration restored as top-level after user feedback 2026-04-19)
- Motion Preview: hidden, not deleted
- Refresh cadence: weekly enrichment, daily prices
- Enrichment storage: `enrichment.db` covers full Scryfall corpus, not just owned cards
- Python async: not used inside Flask routes

---

## Still open (flag to user if blocking)

- Tagger GraphQL endpoint shape — probe first; fallback is search-API enumeration
- Exact Moxfield `api2.moxfield.com/v2` shape — probe first; pin a snapshot
- Exact edhtop16 GraphQL schema — run introspection query first
- Deck-category taxonomy for `target_bin_strategy: by_category` — decide with user when building

---

## Glossary

| Term | Meaning |
|---|---|
| **Enrichment** | Data we pull from external sources (tags, salt, staples, combos, buylists) beyond what Scryfall bulk provides |
| **Preset** | A named, reusable sort configuration (bin → query mapping) |
| **Probe** | A script that calls an external endpoint and asserts response shape matches a pinned snapshot |
| **Coverage** | Percentage of expected data actually pulled (e.g., Tagger: local count / Tagger's stated count) |
| **Staple tier** | Classification of a card's ubiquity: `universal`, `archetype`, `cedh` |
| **Box / divider** | Physical storage: numbered 3D-printed dividers within numbered boxes, used for post-sort location tracking |
| **Cull** | Removing cards from the collection (sell, trade, bulk-out) — filtered by enrichment to find dead weight |

---

## Questions the implementation chat may ask the user

Expected points where an implementation agent will need the user's input:

1. **Tagger auth** — if GraphQL requires login, should we build a session-login helper or fall back to search-API-only?
2. **Deck categorization** — for `by_category` Moxfield imports, what are the bin definitions? (lands / ramp / removal / creatures / win-cons / other?)
3. **Notification channels** — Discord webhook URL? ntfy topic? Test with a sample send before wiring into wishlist events.
4. **Box naming** — does user want auto-generated `box-1`, `box-2` or custom labels? (Plan assumes custom labels allowed; default is auto.)
5. **Staple thresholds** — universal at 5% inclusion, cEDH at 15%. User might want these tuned after seeing real output.
6. **Price refresh source** — Scryfall (included in bulk) is default; user may want MTGGoldfish or MTGStocks later.

Don't guess these when they come up. Ask.
