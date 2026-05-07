An automated Magic: The Gathering card sorting robot. A Marlin-controlled X/Z gantry picks cards from a source bin, stages them for visual identification via a hybrid perceptual-hash / DINOv2 pipeline, and drops each card into a user-configured destination bin. This repo contains the host-side software — motion control, vision, web UI, and data — that drives the hardware from a workshop PC.

Private project. No license is granted. Not for redistribution.

---

## Table of contents

- [What it does](#what-it-does)
- [Architecture](#architecture)
- [Hardware (host's view)](#hardware-hosts-view)
- [Software stack](#software-stack)
- [Repo layout](#repo-layout)
- [Quickstart](#quickstart)
- [Configuration](#configuration)
- [Web UI](#web-ui)
- [Data model](#data-model)
- [Enrichment sources](#enrichment-sources)
- [Identification pipeline](#identification-pipeline)
- [Testing](#testing)
- [Development](#development)
- [Troubleshooting](#troubleshooting)
- [Known issues](#known-issues)
- [Roadmap](#roadmap)
- [Acknowledgments](#acknowledgments)
- [License](#license)

---

## What it does

- **Picks** cards one at a time from a source bin with a vacuum suction cup, places each on a staging platform, and captures a scan.
- **Identifies** each card via a two-model hybrid: perceptual hash (pHash) is the fast primary path against a local Scryfall-derived hash database (~30k oracle IDs); a **DINOv2 ViT-B/14** visual-embedding DB (`card_embeddings.npz`) is a second opinion used when pHash is uncertain. Decision rules live in [card_identify_hybrid.py](card_identify_hybrid.py) — briefly: trust pHash when both agree, when pHash distance ≤ 82, or when pHash has a decisive #1-to-#2 gap at moderate distance; otherwise fall back to DINOv2. No OCR in the primary path — visual matching is faster and more reliable.
- **Sorts** into physical bins based on a user-chosen rule: by color, by price tier, by set, by custom Scryfall query, or by user-authored presets (e.g. "EDH staples", "bulk vs keeper").
- **Logs** every scan to `collection.db` (SQLite) with timestamp, bin assignment, and enrichment metadata.
- **Tracks location** post-sort: which numbered box, which numbered divider. Makes cards findable again without rescanning.

A web UI on `http://localhost:5000` drives the whole thing — live camera feed, sort configuration, session start/stop, and post-sort collection tools.

---

## Architecture

A single card cycle looks like this: the gantry picks a card from the source bin and drops it on the staging platform; the X-carriage-mounted camera moves over the platform and captures a frame; [card_detect.py](card_detect.py) finds the card boundary, [card_identify_hybrid.py](card_identify_hybrid.py) identifies the card via the hybrid pHash / DINOv2 pipeline; the active sort preset resolves a destination bin; and the gantry picks the card back up and drops it in the target bin. Every result is logged to `collection.db`, with enrichment data joined from `enrichment.db`.

Host-side module flow:

```
web_server.py ──► web_worker.py (sort-session state machine)
                         │
    ┌────────────────────┼────────────────────┐
    ▼                    ▼                    ▼
web_camera.py    card_detect.py      card_identify_hybrid.py
                         │
    ┌────────────────────┼────────────────────┐
    ▼                    ▼                    ▼
collection_db.py   enrichment_db.py      config.py
```

For the full hardware-level system diagram, see [PROJECT.md](PROJECT.md).

---

## Hardware (host's view)

The host sees the machine as a Marlin controller over USB serial and an ASUS ROG Eye USB camera mounted on the X carriage. The camera streams portrait-oriented frames and performs both the staging-platform scan and the ArUco-marker bin-calibration sweep. `CROP_SIZE = 745` in [config.py](config.py) matches Scryfall's PNG art width; the hash database depends on it and must not change. Printable calibration markers live in [aruco_markers/](aruco_markers/). For frame, motion, and wiring details, see [PROJECT.md](PROJECT.md).

---

## Software stack

- **Python 3.11+** — Flask + flask_socketio (sync), SQLite, OpenCV, imagehash, pyserial
- **Vision / ML** — PyTorch, DINOv2 ViT-B/14 (visual-embedding second opinion), pillow, numpy
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
├── card_detect.py           Card boundary detection from staging frame
├── card_identify_hybrid.py  Hybrid pHash + DINOv2 identification pipeline
├── foil_detect.py           Foil reflectance detection (experimental)
├── foil_tune.py             Foil threshold tuning utility
├── web_database.py          Scryfall bulk download + hash DB builder
├── web_calibration.py       Camera / gantry calibration routines
├── web_motion_sim.py        Motion preview
├── detection.py             Earlier card boundary detection (scope-fenced)
├── hashing.py               Perceptual hash + DB match (scope-fenced)
├── cards.py / card_lookup.py   Scryfall data models + query helpers
├── collection_db.py         Owned-card database (scan history, storage locations, sessions)
├── enrichment_db.py         External-data database (tags, staples, combos, prices, salt)
├── query_parser.py          Scryfall-query-lite parser used by sort presets
├── preset_store.py          Named sort-config store (bin → query mapping)
├── sort_config.py           Sort-rule configuration models
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

## Quickstart

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

Secrets go in a local `.env` file — loaded via python-dotenv. Template: [`.env.example`](.env.example). Never commit the real `.env` (gitignored).

---

## Configuration

### Environment variables

All are optional unless noted.

- `MOXFIELD_EMAIL` / `MOXFIELD_PASSWORD` — Only needed if enabling collection push to Moxfield; v1 uses public endpoints only for pulls.
- `DISCORD_WEBHOOK_URL` — Optional. Used for Discord notifications in phase 4 features.
- `NTFY_TOPIC` / `NTFY_SERVER` — Optional. Push notifications via ntfy. Defaults to `https://ntfy.sh` if `NTFY_SERVER` is unset.
- `TCGPLAYER_CLIENT_ID` / `TCGPLAYER_CLIENT_SECRET` — Optional. Reserved for future TCGPlayer partner API integration.
- `ENRICHMENT_REFRESH_CRON_*` — Optional cron overrides per source. Suffixes: `TAGGER`, `EDHREC`, `EDHTOP16`, `SPELLBOOK`, `BUYLIST_CK`, `PRICES`. Defaults are weekly or daily depending on the source.

### Hardware constants

- `CROP_SIZE = 745` in [config.py](config.py) — matches Scryfall PNG art width. Changing this invalidates the hash database and requires a full rebuild.

### Data files (all gitignored)

- `collection.db` — owned cards, scan history, storage locations.
- `enrichment.db` — external reference data.
- `scan_logs/` — per-session text logs.
- `card_hashes_v3.json` / `card_embeddings.npz` — built hash and embedding databases.

---

## Web UI

Layout is five tabs:

1. **Dashboard** — camera feed, live scan status, hash DB refresh, data-sources modal.
2. **Bin Setup** — physical bin configuration (count, labels, box assignments).
3. **Sort Session** — pick a preset (every preset shows its per-bin Scryfall query so the mapping is never opaque), start/pause/resume, live per-card info overlay.
4. **Calibration** — ArUco-based automatic bin position calibration, offset baselines, before/after overlay on the live feed.
5. **Collection** — Inventory (filterable grid over scanned cards), Locator (find a card by name → which box + divider), Sync (Moxfield deck import → "pull these from the collection").

Motion Preview is hidden, not deleted.

---

## Data model

Two separate SQLite DBs:

- **`collection.db`** — cards the user actually owns. Scans, sessions, storage locations, box metadata. One row per physical copy.
- **`enrichment.db`** — external reference data covering the full Scryfall oracle corpus (~30k cards), not only owned ones. Tags, salt, combos, staple tiers, buylist prices, price history. Joined to `collection.db` by `oracle_id`.

Keeping enrichment separate means a full refresh never touches ownership data, and the enrichment DB can be regenerated at will.

Both use WAL mode, foreign keys on, `sqlite3.Row` factory. Both files are gitignored.

---

## Enrichment sources

| Source | What | Cadence |
|---|---|---|
| Scryfall bulk | Oracle text, types, mana costs, images, prices | Weekly |
| Scryfall Tagger | `otag:` / `atag:` — functional + art tags | Weekly |
| EDHREC | Inclusion %, salt, theme lists, "overall staples" | Weekly |
| edhtop16 | cEDH staples from tournament decklists | Weekly |
| Commander Spellbook | Combo definitions + card memberships | Weekly |
| CardKingdom buylist | Sell-back prices for cull decisions | Weekly |
| Moxfield (public) | Deck imports during sorting | On demand |

All adapters implement the `EnrichmentSource` interface in [web_enrichment/base.py](web_enrichment/base.py). Each source has a shape-snapshot probe under [probes/](probes/) that fails loudly on API drift.

Full plan: [plans/web_enrichment_plan.md](plans/web_enrichment_plan.md).

---

## Identification pipeline

Each scan runs through the hybrid identifier in [card_identify_hybrid.py](card_identify_hybrid.py):

1. **pHash primary path** — Perceptual hash against `card_hashes_v3.json`. Trust pHash outright when pHash and DINOv2 agree, when the best pHash distance is ≤ 82, or when the #1-to-#2 distance gap is decisive at moderate distance.
2. **DINOv2 second opinion** — If pHash is uncertain, a DINOv2 ViT-B/14 embedding query against `card_embeddings.npz` breaks the tie.

No OCR is used in the primary path — visual matching is faster and more reliable for this domain.

---

## Testing

```bash
# Fast suite (mocked HTTP, no network)
pytest tests/

# Live probes against real external services
pytest tests/ -m live

# Single file
pytest tests/enrichment/test_tagger.py -v

# Targeted run
pytest tests/test_collection_db.py -v
```

Tests use `unittest.TestCase` with `tempfile` fixtures. Conventions are documented in [plans/handoff/10_code_style.md](plans/handoff/10_code_style.md).

Target: full non-live suite under 60s. Existing detection/hashing/motion tests are scope-fenced — don't modify.

---

## Development

- **Branches** — `main` is the baseline. Feature work lands on `feature/*` branches scoped to a milestone.
- **Commits** — Imperative mood (`add X`, not `added`), conventional-commits-ish prefixes (`feat:`, `fix:`, `refactor:`), first line under ~72 characters.
- **Scope fences** — Do not modify without explicit planning: [detection.py](detection.py), [hashing.py](hashing.py), [web_motion_sim.py](web_motion_sim.py), motion control in [web_worker.py](web_worker.py), `calibrate_*.py`, and [config.py](config.py) hardware constants (especially `CROP_SIZE = 745`). See [plans/handoff/04_scope_fences.md](plans/handoff/04_scope_fences.md).
- **Pre-edit safety net** — Before destructive edits, snapshot files to `backup/<timestamp>/`. This directory is gitignored.
- **Long-running guidance** — Project memory and persistent context for Claude Code live under `C:\Users\Jon\.claude\projects\D--Card-Sorter-Scripts\memory\`.

---

## Troubleshooting

- **Card detection returns nothing** — Check that the ROI hasn't been masked into the frame (known regression mode), and verify that `CROP_SIZE` is still 745 in [config.py](config.py).
- **Step skipping on X axis** — Known mechanical issue. Check rod alignment and idler bearing (see [PROJECT.md](PROJECT.md) "Known mechanical issues").
- ~~**E-stop recovery doesn't actually resume**~~ — Fixed 2026-05-07. After Reset & Re-home, press Resume; the continuous loop picks up where it left off. Regression test in [tests/test_estop_recovery.py](tests/test_estop_recovery.py).
- **Hash DB out of date** — Use the Dashboard tab to refresh from Scryfall bulk.
- **Camera frame is upside down or wrong aspect** — Check rotation logic in [web_camera.py](web_camera.py). Do not change `CROP_SIZE` or rotation unless you are prepared to rebuild the hash database from scratch.

---

## Known issues

- ~~**E-stop recovery**~~ — Fixed 2026-05-07. See [tests/test_estop_recovery.py](tests/test_estop_recovery.py).
- **X-axis step skipping** — Mechanical root cause in progress (rod alignment, idler bearing, over-constrained pillow blocks). A higher-torque stepper is on standby if mechanical fixes are insufficient. See [PROJECT.md](PROJECT.md).

---

## Roadmap

- Full enrichment wiring (Scryfall Tagger, EDHREC, edhtop16, Commander Spellbook, CardKingdom buylist)
- Moxfield deck-import flow during sort sessions
- Cull tools (flag vanilla creatures, strictly-worse duplicates)
- Frame / border style detection (Old, Modern, Modern 2.0)
- Foil detection from reflectance patterns
- Condition grading from visual cues (edge wear, scratches, bends)
- Sleeved-card handling
- Ramp-style overflow bin for full-bin scenarios
- Auto-bin-calibration via a second camera on the X carriage

See [PROJECT.md](PROJECT.md) for the full build state and hardware progress.

---

## Acknowledgments

This project relies on external data sources that are unaffiliated with the project:

- **Scryfall** — card data, images, and bulk downloads
- **Scryfall Tagger** — functional and art tags
- **EDHREC** — inclusion statistics and staple lists
- **edhtop16** — cEDH tournament decklist aggregations
- **Commander Spellbook** — combo definitions and memberships
- **CardKingdom** — buylist pricing data

---

## License

Personal project — no license granted. Not for redistribution.
