# Card Sorter — Full Project Overview

A ground-up build: hardware, firmware, and software for automatically sorting a Magic: The Gathering collection. This document covers the whole system. For the host-software specifics (install, run, data model), see [README.md](README.md).

---

## System overview

```
    ┌─────────────────┐   ┌─────────────────────┐   ┌──────────────────┐
    │  Source bin     │   │  Host PC (Windows)  │   │  Web browser     │
    │  (input cards)  │   │                     │   │  localhost:5000  │
    └────────┬────────┘   │  ┌──────────────┐   │   └─────────┬────────┘
             │            │  │ web_server.py│◄──┼─────────────┘
             │            │  │ Flask + IO   │   │
   ┌─────────▼───────┐    │  └──────┬───────┘   │
   │   Gantry X/Z    │    │         │           │
   │  + vacuum head  │    │  ┌──────▼───────┐   │
   │                 │◄───┼──┤ web_worker   │   │
   │  Marlin FW      │ USB│  │ state machine│   │
   │  (serial G-code)│    │  └──────┬───────┘   │
   └────────┬────────┘    │         │           │
            │             │  ┌──────▼───────┐   │
   ┌────────▼────────┐    │  │ detection +  │   │
   │ Staging platform│    │  │ hashing      │   │
   │  + USB camera   │◄───┼──┤              │   │
   │  (on X carriage)│    │  └──────┬───────┘   │
   └────────┬────────┘    │         │           │
            │             │  ┌──────▼───────┐   │
   ┌────────▼────────┐    │  │ collection.db│   │
   │  Numbered bins  │◄───┤  │ enrichment.db│   │
   │  + dividers     │    │  └──────────────┘   │
   └─────────────────┘    └─────────────────────┘
```

The **gantry** picks a card from the source bin, drops it on the staging platform, moves the X-carriage-mounted camera over the platform, the host identifies the card via a hybrid of perceptual hash and DINOv2 visual embeddings, computes a destination bin from the active sort rule, and the gantry picks it back up and drops it there.

---

## Hardware

### Frame

- 4080 aluminum extrusion uprights, ~1 m clear span between them.
- X-axis travel: 1100 mm.
- Build heavily reuses parts from a donor Anet A8 3D printer.

### Linear motion

**X axis** — Two 20 mm smooth steel rods running horizontally between the uprights, carriage riding on LM20UU linear bearings in SC8UU/SCS8UU pillow blocks (4-hole M6 pattern, 40×40 mm). GT2 belt drive.

**Z axis** — The donor Anet A8's entire X-axis assembly, reoriented vertically between the top and bottom X-carriages. Two 8 mm rods (~436 mm long, LM8UU bearings, 46 mm center-to-center). GT2 belt loop, motor on top, idler on bottom. Z travel target ~180–200 mm (enough to reach the bottom of a full 500-card bin).

**No Y axis.** Only X and Z move. The card-handling head and camera both reach over from a fixed Y offset; the source bin, staging platform, and destination bins are all arranged along a single X line.

Design decision: the original Z design used a rack-and-pinion on the moving carriage; it was rejected for center-of-gravity reasons. Reorienting the A8's X assembly keeps the heavy rods fixed to the frame and makes the moving Z mass just the carriage block + printed arm + suction cup.

### Motors + drivers

- **X stepper**: Anet A8 original, NEMA 17, 0.9 A (42SHDC3025-24B). On the edge of capable given the 1100 mm travel and carriage mass — a 1.5–1.7 A replacement is on order as a fallback if step-skipping persists after mechanical fixes land.
- **Z stepper**: Anet A8 original X-axis stepper, NEMA 17, reused with the rest of the A8 X assembly.
- **Drivers**: TMC2209 on all used slots. Sensorless homing was tried and disabled — using wired endstops wired active-LOW.

### Control board + firmware

- **Board**: BTT SKR 1.4 Turbo (NXP LPC1769, 110×85 mm, mounting holes 102×76 mm).
- **Firmware**: Marlin 2.1.3-b1, built with PlatformIO.
- **Key Marlin settings** documented below.
- **Pin repurposing**:
  - Heated bed output (HB) — switches the vacuum pump.
  - HE0 — drives a pressure-release pump that pushes the card off the suction cup at drop.
  - Z-probe pin (P0.10) — G38 contact sensing for the suction-cup landing switch.
  - E0DET / E1DET filament-runout inputs — spare endstop inputs if needed.
- **Configuration.h / pins_*.h customisations** are not in this repo (the firmware project is separate). Rebuilding from stock Marlin won't boot correctly — start from the project's build notes.

### Card-handling head

- One spring-loaded vacuum suction cup on the Z carriage. 60 mm tall total, 30 mm cup diameter, 14 mm barb on top, ~5–10 mm of spring compliance.
- **Two vacuum pumps** on the frame: one for suction (pick-up), one for positive pressure (release / blow-off). Hoses route through the X cable chain to the Z carriage.
- A contact limit switch on the suction cup assembly lets the machine use G38.2 to lower Z until the cup touches the card, rather than moving blind to a fixed depth.
- Workflow: pick card from source bin → drop on staging platform → X carriage moves the camera over the platform to capture a scan → gantry picks the card back up and routes to destination bin → drop.

### Camera

- **Model**: ASUS ROG Eye (gen 1), ~81×17×29 mm.
- **Mount**: on the **X carriage**, on a friction-pivot (single-bolt adjust). Moves horizontally with the head. This is what makes automatic ArUco-based bin-position calibration possible — the camera sweeps along the bin row and records each marker's X position as it passes underneath.
- **Scanning**: after the head drops a card on the staging platform, X moves the camera over the platform and the host captures a frame. Identification then runs through the hybrid pHash + DINOv2 pipeline in [card_identify_hybrid.py](card_identify_hybrid.py).
- Stream native resolution is rotated 90° by the camera manager so the UI receives a portrait-oriented 720×1280 JPEG stream.
- `CROP_SIZE = 745` px in `config.py` — this matches Scryfall's PNG art width and the whole hash database depends on it. Do not change.

### Bins + dividers

- 3D-printed open-top bins. Internal footprint ~66–67 × 91–92 mm (1.5–2 mm clearance per side over a 63 × 88 mm MTG card).
- Capacity ≈ 500 cards (~150 mm stack at ~0.3 mm per card).
- Cards drop in from above when vacuum releases — no moving ramp, no ejection mechanism. An earlier design with a moving bin platform was discarded.
- **Double-feed prevention**: simple corner wedges/nubs printed into each bin's inner walls. The picked card flexes past the nubs and any duplicate card underneath stays put. Alternatives considered and rejected: brush bristles (risk of edge delamination), TPU bands (tuning nightmare across card conditions), separate clip-on TPU flaps (too fiddly).
- Numbered 3D-printed dividers inside each bin subdivide storage for the post-sort Locator sub-view — every card gets a repeatable "box N, divider M" address.
- Bin fullness is tracked purely in software (count vs. configured limit). No hardware limit switches on bins, none planned.

### Lighting

- Planned: a ~1 m warm-white LED strip in a V-shaped aluminum channel with a snap-in diffuser, mounted on printed brackets offset from the 4080 uprights so it clears the X carriage.
- High-CRI (90+), neutral or warm white (4000–5000 K) — better color rendition than bluish-cold addressable strips.
- Spans the full working area so both the staging-platform scan point and the bin row's ArUco markers are lit evenly. ArUco detection quality is the primary driver.
- Wired to the PSU directly (simple on/off switch) or optionally to an SKR fan header for G-code control. Not yet installed at time of writing.

### Calibration markers

Calibration uses ArUco markers:

- IDs **0–9** — source-side markers (staging tray reference, input bin positions)
- IDs **10–49** — destination-side markers (bin positions)
- ID **49** — staging platform reference marker

Printable marker PNGs are in [aruco_markers/](aruco_markers/). A calibration sweep drives X across the bin row, detects markers frame-by-frame, and records each bin's absolute X coordinate. Once the X-carriage camera mount is wired in, this becomes fully unattended.

### Known mechanical issues (in progress)

- **X-axis step skipping**: root cause trace in progress — rod non-parallelism over 1100 mm, over-constrained pillow blocks, an idler running on a gear instead of a bearing, and a motor/belt line offset ~25 mm from the rod centerline. Mechanical fixes (rod alignment, proper idler bearing 686ZZ or 625ZZ) come first; the new stepper is a fallback.
- **X-carriage squeaking**: belt pulls the carriage at a point that's offset from its two pillow-block supports. Planned fix: third pillow block near the belt attachment.

### Parts & sourcing

- Donor machine: Anet A8 3D printer (X assembly, both X and Z steppers, belts, bearings).
- Control board: BigTreeTech SKR 1.4 Turbo.
- Frame: 4080 and (possibly) 2020 aluminum extrusion.
- Linear hardware: LM20UU and LM8UU bearings, SC8UU/SCS8UU pillow blocks, 20 mm and 8 mm smooth rods, GT2 belt.
- Vision: ASUS ROG Eye (gen 1).

Build log / BOM with exact vendor links lives outside this repo.

---

## Firmware (Marlin)

The machine runs a modified Marlin 2.1.3-b1 build targeting the SKR 1.4 Turbo (NXP LPC1769, `env:nxp_lpc1769` in PlatformIO). The firmware project itself is not in this repo, but the configuration choices that matter for the host software are:

| Setting | Value | Why |
|---|---|---|
| `MOTHERBOARD` | `BOARD_BTT_SKR_V1_4_TURBO` | Match the control board |
| `EXTRUDERS` | `0` | No hotend; this isn't a 3D printer |
| `Z_SAFE_HOMING` | off | Not applicable — no bed |
| `SENSORLESS_HOMING` | off | Physical wired endstops are more reliable here |
| Endstop logic | active-LOW | Matches the wired switches |
| Stepper drivers | TMC2209 (TMCStepper 0.8.0) | UART control, standstill current reduction |
| `HOST_ACTION_COMMANDS` | on | Lets the host recover cleanly from errors |

Custom pin mappings live in `pins_BTT_SKR_V1_4.h` overrides (not in this repo):
- **HB** (heated bed output) → suction vacuum pump
- **HE0** → release / positive-pressure pump
- **Z-probe input (P0.10)** → suction-cup contact limit switch for G38.2 probes
- **E0DET / E1DET** → available as spare endstop inputs

Rebuilding from stock Marlin will not boot correctly. Start from the project's build notes.

### Host ↔ firmware protocol

The host talks to Marlin over USB serial, sending plain G-code. Critical rules enforced by `web_worker.py`:

- **Never overlap Z and X moves.** Z must complete (`M400` sync) before X starts. The physical geometry is such that a simultaneous Z-down + X-move can crash the head.
- Every multi-step move ends with `M400` before the host proceeds.
- E-stop uses Marlin's kill / emergency stop; recovery requires an explicit re-home from the UI.

---

## Software (host)

The host software is a single Python application in this repo — Flask web server, SocketIO for live updates, SQLite for data, OpenCV + imagehash + PyTorch/DINOv2 for identification, pyserial for Marlin comms.

Deep dive: [README.md](README.md).

Architecture in one paragraph: a Flask process (`web_server.py`) serves a Bootstrap single-page app and a WebSocket. A worker module (`web_worker.py`) owns the sort-session state machine, reads frames from the camera thread (`web_camera.py`), runs contour-based card detection (`card_detect.py`), and identifies each card through the hybrid pipeline in `card_identify_hybrid.py` — perceptual hash first (against `card_hashes_v3.json`), DINOv2 ViT-B/14 embeddings as a second opinion (against `card_embeddings.npz`) when pHash is uncertain. The result is looked up against per-card enrichment data (tags, staples, prices) to resolve a destination bin, and G-code is issued to Marlin to route the card. Everything observable — scan results, bin fullness, errors — is mirrored to the browser over SocketIO.

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

- End-to-end sort pipeline (staging → scan → identify → route → drop) with hybrid pHash + DINOv2 identification
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

- ~~**E-stop recovery is incomplete.**~~ Fixed 2026-05-07. `_cmd_reset_after_estop` now restores `continuous_sorting` from the pre-estop snapshot so the user's Resume click re-arms the continuous loop. Regression test in [tests/test_estop_recovery.py](tests/test_estop_recovery.py).

---

## Principles / non-obvious decisions

A few choices that look arbitrary but aren't, captured so they survive context loss:

- **Visual matching, not OCR.** OCR was tried as the primary ID method and is not reliable enough. Perceptual hash is the fast primary path; DINOv2 visual embeddings are a second opinion for cases pHash gets wrong (foils, elevated lighting variation, near-duplicate art). OCR is at best a tiebreaker and not in the live path.
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
