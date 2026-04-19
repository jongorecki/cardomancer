# Card Sorter

An automated Magic: The Gathering card sorting robot. A Marlin-controlled gantry moves cards from a staging area past a fixed camera, identifies each card by perceptual hash, and drops it into a user-configured bin.

Private project. Runs on the workshop PC that drives the hardware.

---

## What it does

- **Scans** cards one at a time from a staging tray.
- **Identifies** each card via perceptual-hash match against a local Scryfall-derived hash database (~30k oracle IDs). No OCR in the primary path — hashing is faster and more reliable.
- **Sorts** into physical bins based on a user-chosen rule: by color, by price tier, by set, by custom Scryfall query, or by user-authored presets (e.g. "EDH staples", "bulk vs keeper").
- **Logs** every scan to `collection.db` (SQLite) with timestamp, bin assignment, and enrichment metadata.
- **Tracks location** post-sort: which numbered box, which numbered divider. Makes cards findable again without rescanning.

A web UI on `http://localhost:5000` drives the whole thing — live camera feed, sort configuration, session start/stop, and post-sort collection tools.

---

## Hardware

- Custom Marlin-firmware gantry (X/Y/Z). Endstops LOW, EXTRUDERS 0, `Z_SAFE_HOMING` off, sensorless homing off — see `config.py` for the working pin map.
- USB camera fixed above the scan position; `CROP_SIZE = 745` matches Scryfall PNG art size (do not change).
- 3D-printed numbered bin dividers for post-sort tracking.

**This repo doesn't include firmware builds or CAD** — it's the host-side software only (motion control, vision, web UI, data).

---

## Software stack

- **Python 3.11+** — Flask + flask_socketio (sync), SQLite, OpenCV, imagehash, pyserial
- **Web UI** — vanilla JS + Bootstrap 5 (no build step, no framework)
- **Enrichment** — httpx, gql, APScheduler, pydantic, python-dotenv, beautifulsoup4

Full pins in [requirements.txt](requirements.txt).

---

## Repo layout

```
Scripts/
├── web_server.py            Flask + SocketIO entry point (serves UI on :5000)
├── web_worker.py            Sort-session state machine (scan → identify → route → drop)
├── web_camera.py            Camera capture + streaming
├── web_database.py          Scryfall bulk download + hash DB builder
├── web_calibration.py       Camera / gantry calibration routines
├── web_motion_sim.py        Motion preview (hidden in UI; still present)
├── detection.py             Card boundary detection from frame (scope-fenced)
├── hashing.py               Perceptual hash + DB match (scope-fenced)
├── cards.py / card_lookup.py   Scryfall data models + query helpers
├── collection_db.py         Owned-card database (scan history, storage locations, sessions)
├── enrichment_db.py         External-data database (tags, staples, combos, prices, salt)
├── query_parser.py          Scryfall-query-lite parser used by sort presets
├── preset_store.py          Named sort-config store (bin → query mapping)
├── config.py                Hardware constants (CROP_SIZE, camera offsets, marker IDs)
├── web_enrichment/          External-source adapters (Tagger, EDHREC, edhtop16, Spellbook, ...)
├── probes/                  Schema-shape probes per external source
├── sort_configs/            User-authored preset files
├── tests/                   pytest suite (unittest-style TestCase)
├── templates/index.html     Single-page web UI (Bootstrap tabs)
├── static/app.js            All front-end JS in one file
├── static/style.css         All CSS (theme variables, no hardcoded colors)
├── plans/                   Feature planning + handoff docs for implementation agents
└── backup/                  Pre-edit file snapshots (local safety net, not for tracking)
```

---

## Running

Prerequisites: Python 3.11+, the hardware connected on the expected serial port, and a built hash database.

```bash
# Install deps
pip install -r requirements.txt

# First-time: download Scryfall bulk + build the hash DB
# (See web_database.py; also exposed from the Dashboard tab after the server starts.)

# Start the web server
python web_server.py
# → http://localhost:5000
```

Secrets (Moxfield creds, Discord webhook, ntfy topic) go in a local `.env` file — loaded via python-dotenv. Template: `.env.example`. Never commit the real `.env` (gitignored).

---

## Web UI tabs

After the Phase 0A consolidation, four tabs:

1. **Dashboard** — camera feed, live scan status, hash DB refresh, calibration entry points.
2. **Sort Session** — pick a preset (every preset shows its per-bin Scryfall query so the mapping is never opaque), start/pause/resume, live per-card info overlay.
3. **Collection** — Inventory (filterable grid over scanned cards), Locator (find a card by name → which box + divider), Sync (Moxfield deck import → "pull these from the collection").
4. **Bin Setup** — physical bin configuration (count, labels, box assignments).

Motion Preview is hidden, not deleted.

---

## Data model

Two separate SQLite DBs:

- **`collection.db`** — cards the user actually owns. Scans, sessions, storage locations, box metadata. One row per physical copy.
- **`enrichment.db`** — external reference data covering the full Scryfall oracle corpus (~30k cards), not only owned ones. Tags, salt, combos, staple tiers (universal / archetype / cEDH), combo memberships, buylist prices, price history. Joined to `collection.db` by `oracle_id`.

Keeping enrichment separate means a full refresh never touches ownership data, and the enrichment DB can be regenerated at will.

Both use WAL mode, foreign keys on, `sqlite3.Row` factory.

---

## Enrichment sources (in progress)

| Source | What | Cadence |
|---|---|---|
| Scryfall bulk | Oracle text, types, mana costs, images, prices | Weekly |
| Scryfall Tagger | `otag:` / `atag:` — functional + art tags | Weekly |
| EDHREC | Inclusion %, salt, theme lists, "overall staples" | Weekly |
| edhtop16 | cEDH staples from tournament decklists | Weekly |
| Commander Spellbook | Combo definitions + card memberships | Weekly |
| CardKingdom buylist | Sell-back prices for cull decisions | Weekly |
| Moxfield (public) | Deck imports during sorting | On demand |

All adapters share the `EnrichmentSource` interface in [web_enrichment/base.py](web_enrichment/base.py). Each has a shape-snapshot probe under [probes/](probes/) that fails loudly on API drift.

Full plan: [plans/web_enrichment_plan.md](plans/web_enrichment_plan.md).

---

## Testing

```bash
# Fast suite (mocked HTTP, no network)
pytest tests/

# Live probes against real external services
pytest tests/ -m live

# Single file
pytest tests/enrichment/test_tagger.py -v
```

Target: full non-live suite under 60s. Existing detection/hashing/motion tests are scope-fenced — don't modify.

---

## Scope fences

Some files are load-bearing and not to be touched without a very good reason:

- `detection.py`, `hashing.py` — the vision pipeline
- `web_motion_sim.py`, motion control in `web_worker.py`
- `calibrate_*.py`, `config.py` hardware constants (especially `CROP_SIZE = 745`)

See [plans/handoff/04_scope_fences.md](plans/handoff/04_scope_fences.md).

---

## Branch layout

- `main` — baseline
- `phase0a-foundation` — DB schema, tab consolidation, probe scaffolding
- `phase0b-probes` — preset store + real shape-asserting probes

Phase 1–4 branches land per feature as work continues.

Commit style: imperative mood (`add X`, not `added`), first line under ~60 chars.

---

## Known issues

- **E-stop recovery** — "Reset & Re-home" after an E-stop does not actually resume the in-flight session. Likely a stale abort flag or missing state transition in `_cmd_reset_after_estop`.

---

## License

Personal project — no license granted. Not for redistribution.
