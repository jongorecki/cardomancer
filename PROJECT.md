# Card Sorter — Full Project Overview

A ground-up build: hardware, firmware, and software for automatically sorting a Magic: The Gathering collection. This document covers the whole system. For the host-software specifics (install, run, data model), see [README.md](README.md).

---

## System overview

```
    ┌─────────────────┐   ┌─────────────────────┐   ┌──────────────────┐
    │  Staging tray   │   │  Host PC (Windows)  │   │  Web browser     │
    │  (input cards)  │   │                     │   │  localhost:5000  │
    └────────┬────────┘   │  ┌──────────────┐   │   └─────────┬────────┘
             │            │  │ web_server.py│◄──┼─────────────┘
             │            │  │ Flask + IO   │   │
   ┌─────────▼───────┐    │  └──────┬───────┘   │
   │    Gantry X/Y/Z │    │         │           │
   │   + vacuum head │    │  ┌──────▼───────┐   │
   │                 │◄───┼──┤ web_worker   │   │
   │  Marlin FW      │ USB│  │ state machine│   │
   │  (serial G-code)│    │  └──────┬───────┘   │
   └────────┬────────┘    │         │           │
            │             │  ┌──────▼───────┐   │
   ┌────────▼────────┐    │  │ detection +  │   │
   │   USB camera    │◄───┼──┤ hashing      │   │
   │  (fixed above   │    │  └──────┬───────┘   │
   │   scan point)   │    │         │           │
   └─────────────────┘    │  ┌──────▼───────┐   │
                          │  │ collection.db│   │
   ┌─────────────────┐    │  │ enrichment.db│   │
   │  Numbered bins  │◄───┤  └──────────────┘   │
   │  + dividers     │    │                     │
   └─────────────────┘    └─────────────────────┘
```

The **gantry** picks a card from the staging tray, moves it into the camera's view, the host identifies it via perceptual hash, computes a destination bin from the active sort rule, and the gantry drops it there.

---

## Hardware

### Gantry

A custom X/Y/Z motion platform driven by stepper motors, closer in design to a small 3D printer or CNC than a pick-and-place. Movements:

- **X** — traverses across the bin row
- **Y** — reaches between staging and bins
- **Z** — lifts and lowers the card-handling head

Homing order and travel speeds live in [config.py](config.py) and the Marlin firmware build. Sensorless homing is intentionally off — the machine uses physical endstops wired active-LOW.

### Card-handling head

The head mounts to the Z axis and picks/places cards one at a time. Cards are moved from the staging tray → scan position → destination bin.

### Camera

A single USB camera is fixed above the scan position, pointed down. The card is paused under the camera for identification. Key constant: `CROP_SIZE = 745` pixels — this matches Scryfall's PNG art export width, and the whole hash database depends on it. Don't change it.

### Bins + dividers

Cards drop into numbered bins. Inside each bin, 3D-printed numbered dividers subdivide storage so each physical card has a repeatable post-sort address: **box N, divider M**. The web UI's Locator sub-view uses this to find a specific card later without rescanning.

Bin fullness is tracked in software (count of cards routed there this session vs. a configured capacity) — there are no hardware limit switches on the bins themselves, and none planned.

### Calibration markers

Calibration uses ArUco markers:

- IDs **0–9** — source-side markers (staging tray reference)
- IDs **10–49** — destination-side markers (bin positions)

Printable marker PNGs are in [aruco_markers/](aruco_markers/). A calibration sweep in the web UI drives X across the bin row, detects markers frame-by-frame, and records each bin's absolute X coordinate. A future improvement puts a camera on the X carriage so this can happen fully unattended.

### Parts & sourcing

Intentionally unspecified here — build is custom. See hardware notes / build log (not in this repo) for BOM details.

---

## Firmware (Marlin)

The machine runs a modified Marlin configuration. The firmware source itself is not in this repo (it's built separately with PlatformIO), but the **configuration choices that matter** are documented here because they interact with the host software:

| Setting | Value | Why |
|---|---|---|
| `EXTRUDERS` | `0` | No hotend; this isn't a 3D printer |
| `Z_SAFE_HOMING` | off | Not applicable — no bed |
| `SENSORLESS_HOMING` | off | Physical endstops are more reliable for this build |
| Endstop logic | active-LOW | Matches the wired switches |
| `HOST_ACTION_COMMANDS` | on | Lets the host recover from errors cleanly |

Custom pin mappings: handled in the board's `pins_*.h` file. Recorded in the build notes (external). If rebuilding firmware, start from those notes — Marlin defaults do not work.

### Host ↔ firmware protocol

The host talks to Marlin over USB serial, sending plain G-code. Critical rules enforced by `web_worker.py`:

- **Never overlap Z and X moves.** Z must complete (`M400` sync) before X starts. The physical geometry is such that a simultaneous Z-down + X-move can crash the head.
- Every multi-step move ends with `M400` before the host proceeds.
- E-stop uses Marlin's kill / emergency stop; recovery requires an explicit re-home from the UI.

---

## Software (host)

The host software is a single Python application in this repo — Flask web server, SocketIO for live updates, SQLite for data, OpenCV + imagehash for identification, pyserial for Marlin comms.

Deep dive: [README.md](README.md).

Architecture in one paragraph: a Flask process (`web_server.py`) serves a Bootstrap single-page app and a WebSocket. A worker module (`web_worker.py`) owns the sort-session state machine, reads frames from the camera thread (`web_camera.py`), runs detection (`detection.py`) and hashing (`hashing.py`), looks up matches against a local hash database built from Scryfall bulk data, consults per-card enrichment data (tags, staples, prices) to resolve a destination bin, and issues G-code to Marlin to route the card. Everything observable — scan results, bin fullness, errors — is mirrored to the browser over SocketIO.

---

## Data

Two SQLite databases, deliberately separated:

- **`collection.db`** — ground truth about *your* cards. One row per physical copy scanned, plus sessions, storage locations, box metadata. Never regenerated from outside data.
- **`enrichment.db`** — external reference data covering the full Scryfall oracle corpus (~30k cards, not just owned ones). Tags, salt scores, combo memberships, staple tiers, buylist prices. Regenerable at will via the refresh scheduler.

Join key: `oracle_id`. Both DBs are gitignored — data files never get committed.

See [plans/handoff/07_shared_interfaces.md](plans/handoff/07_shared_interfaces.md) for full schemas.

---

## Build state

### Working today

- End-to-end sort pipeline (staging → scan → identify → route → drop) with hash-based identification
- Web UI with live camera, session control, and preset-based sort configuration
- Calibration routines (camera height, bin X positions via ArUco sweep)
- Collection database with scan history and storage-location tracking
- Enrichment foundation: DB schema, source adapters (stubs), shape probes, scheduler (Phase 0A/0B complete)
- E-stop that kills motion safely

### In progress

- Full enrichment wiring: real refreshes from Scryfall Tagger, EDHREC, edhtop16, Commander Spellbook, CardKingdom buylist
- Moxfield deck-import flow ("pull these cards from the collection") during sort sessions
- Cull tools (flag vanilla creatures, strictly-worse duplicates)

### Planned

- Frame / border style detection (Old vs. Modern vs. Modern 2.0, etc.) — high priority
- Foil detection from reflectance pattern in scan frames
- Condition grading from visual cues (edge wear, scratches, bends)
- Sleeved-card handling
- Ramp-style overflow bin for when all bins are full
- Auto-bin-calibration via a second camera on the X carriage reading markers inside each bin

### Known bugs

- **E-stop recovery is incomplete.** "Reset & Re-home" after an E-stop does not resume the in-flight session; likely a stale abort flag or missing state transition in `_cmd_reset_after_estop`. Logged in project memory.

---

## Principles / non-obvious decisions

A few choices that look arbitrary but aren't, captured so they survive context loss:

- **Hash, don't OCR.** OCR was tried as the primary ID method and is not reliable enough. Perceptual hash is the primary path; OCR is at best a tiebreaker.
- **Full-corpus enrichment, not owned-only.** Storing enrichment for all ~30k oracle IDs (not just owned cards) means you can act on card info *before* the card is scanned — e.g. import a Moxfield deck and the system already knows which of those cards are staples.
- **Two databases, not one.** `collection.db` is irreplaceable (your scans). `enrichment.db` is disposable (rebuild from sources). Mixing them would make refresh dangerous.
- **No framework on the front end.** `static/app.js` is one file of vanilla JS + Bootstrap. Simpler to debug, no build step, no dependency churn.
- **`backup/<timestamp>/` plus git.** Belt and suspenders. Git gives real history; `backup/` protects against accidental destructive git operations. The `backup/` tree is gitignored for new snapshots.

---

## Directory map

```
Scripts/                      ← this repo (host software)
├── *.py                      ← Python modules (web server, worker, vision, DB)
├── templates/index.html      ← single-page web UI
├── static/{app.js,style.css} ← front-end
├── sort_configs/             ← user-authored sort presets
├── web_enrichment/           ← external-source adapters
├── probes/                   ← shape probes for each external source
├── tests/                    ← pytest suite
├── plans/                    ← feature planning + handoff docs for agents
├── aruco_markers/            ← printable calibration markers
├── backup/                   ← pre-edit file snapshots (local safety net)
├── requirements.txt
├── config.py                 ← hardware + pipeline constants
├── README.md                 ← developer-focused readme
└── PROJECT.md                ← this file

NOT in this repo (external):
├── Marlin firmware build     ← PlatformIO project with modified Configuration.h
├── CAD / STL files           ← gantry frame, card-handling head, bin dividers
└── Build-log / BOM notes     ← hardware sourcing
```

---

## Getting started — if you're me, in six months, and forgot

1. Power on the machine. Confirm USB serial connects.
2. `cd D:/Card_Sorter/Scripts && python web_server.py`
3. Open `http://localhost:5000`. Camera feed should be live.
4. Dashboard → re-home the gantry. Confirm no crashes.
5. If the hash DB is stale, Database sub-view → refresh from Scryfall bulk.
6. Sort Session tab → pick a preset, hit Start, load cards in staging.
7. If anything is unexpected, read `scan_logs/<latest>.log` before changing anything.
