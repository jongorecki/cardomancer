# Shared Interfaces

Classes, function signatures, and contracts that multiple features share. Define these once in Phase 0A so every feature plugs in uniformly.

---

## EnrichmentSource (abstract base)

Every external-data source (Tagger, EDHREC, edhtop16, Spellbook, buylist vendors) implements this contract.

```python
# web_enrichment/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

@dataclass
class RefreshResult:
    source: str
    success: bool
    duration_ms: int
    rows_changed: int
    coverage_pct: float  # 0.0–100.0
    errors: list[str]
    warnings: list[str]


class EnrichmentSource(ABC):
    """Contract for every external data source."""

    name: str  # e.g., "tagger", "edhrec", "edhtop16"

    @abstractmethod
    def probe(self) -> bool:
        """Quick health check. Returns True if endpoint reachable + shape unchanged.
        Must not mutate any data. Must complete in < 5s.
        """
        raise NotImplementedError

    @abstractmethod
    def refresh(self, emit: Optional[Callable[[str, dict], None]] = None,
                full: bool = False) -> RefreshResult:
        """Pull latest data into enrichment.db.

        Args:
            emit: callback for progress events. Signature: emit(event_name, data_dict).
                  Emits 'enrichment_refresh_progress' with step/progress/total/message.
            full: if True, ignore incremental state and re-pull everything.

        Returns:
            RefreshResult with success/failure and coverage metrics.

        Implementation MUST:
          - Write to a temp table or use a transaction; never partial-commit on error.
          - Update sync_metadata on every call (success or failure).
          - Write coverage_reports rows for any per-key coverage claims.
          - Respect rate limits for the source.
          - Check probe() first; abort if probe fails.
        """
        raise NotImplementedError

    @abstractmethod
    def coverage_report(self) -> dict:
        """Current coverage state. Fast — reads from enrichment.db only."""
        raise NotImplementedError
```

Concrete sources live under `web_enrichment/`:
- `web_enrichment/tagger.py: class TaggerSource(EnrichmentSource)`
- `web_enrichment/edhrec.py: class EDHRECSource(EnrichmentSource)`
- `web_enrichment/edhtop16.py: class EDHTop16Source(EnrichmentSource)`
- `web_enrichment/spellbook.py: class SpellbookSource(EnrichmentSource)`
- `web_enrichment/buylist_ck.py: class CardKingdomBuylistSource(EnrichmentSource)`

---

## EnrichmentRepo (data access layer)

Single class that every feature queries for enrichment data. Never write raw enrichment-table SQL outside this class.

```python
# web_enrichment/repo.py
import sqlite3
from dataclasses import dataclass
from typing import Optional

@dataclass
class CardEnrichment:
    oracle_id: str
    tags: list[str]
    art_tags_by_printing: dict[str, list[str]]
    staples: dict  # {universal: bool, cedh: bool, archetype: bool, archetype_count: int, archetypes: list[str]}
    salt: Optional[float]
    combos: list[dict]
    buylists: dict[str, float]  # {vendor: price_usd}
    themes: list[str]
    commander_rank: Optional[int]
    source_freshness: dict[str, str]  # {source_name: "fresh" | "stale_Nd" | "missing"}


class EnrichmentRepo:
    """Read-only access to enrichment.db. Writers use EnrichmentSource directly."""

    def __init__(self, db_path: str):
        self.db_path = db_path

    def get_card(self, oracle_id: str) -> Optional[CardEnrichment]:
        """Full enrichment for one card. Returns None if oracle_id unknown."""

    def get_tags(self, oracle_id: str) -> list[str]:
        """Just the function tags."""

    def query(self, scryfall_query: str, *, limit: int = 50, offset: int = 0,
              owned_only: bool = False) -> tuple[list[dict], int]:
        """Run an enrichment-aware query (supports otag:, atag:, staple:, salt>, combo:).

        Returns (cards, total_count). Cards are dicts with basic fields + enrichment summary.
        owned_only=True joins against collection.db inventory.
        """

    def get_staples(self, tier: str) -> list[str]:
        """Return oracle_ids for a staple tier. tier ∈ {universal, archetype, cedh}."""

    def get_combo_members(self, combo_id: str) -> list[str]:
        """Return oracle_ids that make up a combo."""

    def coverage_overview(self) -> dict:
        """Per-source coverage for the /api/enrichment/sources endpoint."""

    # Internal: joins collection.db via ATTACH DATABASE
    def _with_collection(self, conn: sqlite3.Connection) -> sqlite3.Connection:
        """ATTACH the collection.db so queries can JOIN inventory."""
```

Instantiated once at server startup, passed to features via dependency injection (pass the instance into `web_server.py` globals, same pattern as `worker`, `camera`, etc.).

---

## RefreshScheduler

Wraps APScheduler with source registration and event emission.

```python
# web_enrichment/scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler
from typing import Callable

class RefreshScheduler:
    def __init__(self, emit: Callable[[str, dict], None]):
        self._sched = BackgroundScheduler()
        self._sources: dict[str, EnrichmentSource] = {}
        self._emit = emit

    def register(self, source: EnrichmentSource, cron: str) -> None:
        """Register a source with a cron expression. Example cron: 'weekly' | 'daily' | '0 3 * * 0'."""

    def start(self) -> None:
        self._sched.start()

    def shutdown(self) -> None:
        self._sched.shutdown()

    def trigger(self, source_name: str, full: bool = False) -> None:
        """Manually trigger a refresh (from API endpoint). Runs in a background thread,
        emits progress via self._emit."""

    def list_sources(self) -> list[dict]:
        """Sources + their next-run-time and last-run state."""
```

Registered in `web_server.py` startup:

```python
scheduler = RefreshScheduler(emit=lambda e, d: socketio.emit(e, d))
scheduler.register(TaggerSource(), cron="weekly")
scheduler.register(EDHRECSource(), cron="weekly")
scheduler.register(EDHTop16Source(), cron="weekly")
scheduler.register(SpellbookSource(), cron="weekly")
scheduler.register(CardKingdomBuylistSource(), cron="daily")
scheduler.start()
atexit.register(scheduler.shutdown)
```

---

## ProbeResult

Uniform result shape for `probes/probe_<source>.py` scripts and the `/api/enrichment/probes/run` endpoint.

```python
# probes/base.py
from dataclasses import dataclass

@dataclass
class ProbeResult:
    source: str
    ok: bool
    endpoint: str
    duration_ms: int
    shape_diff: list[str]  # empty if ok
    warnings: list[str]
    snapshot_path: Optional[str]  # where the latest response was saved
    pinned_path: Optional[str]    # pinned reference
```

Every probe script exposes:
```python
def probe() -> ProbeResult: ...

if __name__ == "__main__":
    r = probe()
    print(r)
    exit(0 if r.ok else 1)
```

`probes/run_all.py` imports and calls each `probe()`, collects `ProbeResult`s, writes a summary.

---

## Emit callback contract

The existing `set_emit(callback)` pattern used by `worker`, `calibrator`, `db_updater` is the norm. New modules follow it:

```python
# In any emit-capable class:
class MyModule:
    def __init__(self):
        self._emit_fn = None

    def set_emit(self, emit_fn: Callable[[str, dict], None]) -> None:
        self._emit_fn = emit_fn

    def _emit(self, event: str, data: dict) -> None:
        if self._emit_fn:
            self._emit_fn(event, data)
```

In `web_server.py`:
```python
enrichment_manager.set_emit(lambda e, d: socketio.emit(e, d))
```

Event naming:
- All lowercase-snake-case.
- Start with source/module name: `tagger_refresh_progress`, `edhrec_refresh_complete`, `moxfield_import_progress`, `storage_divider_advanced`, `wishlist_match`.
- Progress events carry `{step, progress, total, message, source}`.
- Completion events carry `{source, duration_ms, rows_changed, coverage_pct, errors, ts}`.
- All events include `ts` (ISO 8601 UTC) so clients can discard stale.

---

## enrichment.db schema (canonical)

Spelled out here so every feature agrees. Create via `enrichment_db.py: _create_tables(conn)` in Phase 0A.

```sql
-- One row per (oracle_id, tag). Populated by TaggerSource.
CREATE TABLE IF NOT EXISTS tags (
    oracle_id TEXT NOT NULL,
    tag_name TEXT NOT NULL,
    source TEXT NOT NULL,  -- 'scryfall_search' | 'hierarchy' | 'local'
    PRIMARY KEY (oracle_id, tag_name)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag_name);

-- Art tags keyed on printing id.
CREATE TABLE IF NOT EXISTS art_tags (
    printing_id TEXT NOT NULL,
    tag_name TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (printing_id, tag_name)
);
CREATE INDEX IF NOT EXISTS idx_art_tags_tag ON art_tags(tag_name);

-- All known tags (function + art) with hierarchy + expected counts.
CREATE TABLE IF NOT EXISTS tag_catalog (
    tag_name TEXT PRIMARY KEY,
    tag_type TEXT NOT NULL,  -- 'function' | 'art'
    parent TEXT,
    description TEXT,
    card_count_expected INTEGER,
    source TEXT NOT NULL     -- 'tagger' | 'fallback'
);

-- Staples per tier per source. A card can have multiple rows.
CREATE TABLE IF NOT EXISTS staples (
    oracle_id TEXT NOT NULL,
    tier TEXT NOT NULL,      -- 'universal' | 'archetype' | 'cedh'
    source TEXT NOT NULL,    -- 'edhrec' | 'edhtop16'
    score REAL,              -- inclusion rate 0–1
    archetypes_json TEXT,    -- JSON array for archetype tier
    last_updated TEXT,
    PRIMARY KEY (oracle_id, tier, source)
);

CREATE TABLE IF NOT EXISTS salt_scores (
    oracle_id TEXT PRIMARY KEY,
    salt REAL,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS themes (
    oracle_id TEXT NOT NULL,
    theme_name TEXT NOT NULL,
    source TEXT NOT NULL,
    inclusion_pct REAL,
    PRIMARY KEY (oracle_id, theme_name, source)
);

CREATE TABLE IF NOT EXISTS combos (
    combo_id TEXT PRIMARY KEY,
    result TEXT,
    identity TEXT,
    mana_needed TEXT,
    prerequisites_json TEXT,
    source TEXT NOT NULL DEFAULT 'spellbook'
);

CREATE TABLE IF NOT EXISTS combo_membership (
    oracle_id TEXT NOT NULL,
    combo_id TEXT NOT NULL,
    quantity INTEGER DEFAULT 1,
    PRIMARY KEY (oracle_id, combo_id),
    FOREIGN KEY (combo_id) REFERENCES combos(combo_id)
);

CREATE TABLE IF NOT EXISTS commander_ranks (
    oracle_id TEXT PRIMARY KEY,
    deck_count INTEGER,
    avg_synergy_json TEXT,
    source TEXT NOT NULL,
    last_updated TEXT
);

CREATE TABLE IF NOT EXISTS buylists (
    oracle_id TEXT NOT NULL,
    vendor TEXT NOT NULL,    -- 'ck' | 'tcg' | 'cardconduit'
    price_usd REAL,
    last_updated TEXT,
    PRIMARY KEY (oracle_id, vendor)
);

CREATE TABLE IF NOT EXISTS price_history (
    oracle_id TEXT NOT NULL,
    date TEXT NOT NULL,
    market_usd REAL,
    source TEXT NOT NULL,    -- 'scryfall' | 'mtgstocks' (future)
    PRIMARY KEY (oracle_id, date, source)
);

-- Manual user-added tags, OR'd with tags on read.
CREATE TABLE IF NOT EXISTS local_tags (
    oracle_id TEXT NOT NULL,
    tag_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (oracle_id, tag_name)
);

-- One row per source per refresh. Latest row = current state.
CREATE TABLE IF NOT EXISTS sync_metadata (
    source TEXT PRIMARY KEY,
    last_success TEXT,
    last_attempt TEXT,
    version_hash TEXT,
    error TEXT,
    coverage_pct REAL
);

-- Per-refresh, per-key coverage diffs. Append-only.
CREATE TABLE IF NOT EXISTS coverage_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    run_at TEXT NOT NULL,
    key_name TEXT,           -- tag name, combo id, etc.
    expected INTEGER,
    actual INTEGER,
    diff_json TEXT
);

-- Seed: one row per oracle_id in Scryfall bulk, used as the card universe.
CREATE TABLE IF NOT EXISTS card_universe (
    oracle_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    last_seen_bulk TEXT
);
```

---

## collection.db additions

Already covered in `web_enrichment_plan.md`. Spelled out here for Phase 0A:

```sql
CREATE TABLE IF NOT EXISTS storage_locations (
    scan_id INTEGER PRIMARY KEY,
    box_id TEXT NOT NULL,
    divider_id INTEGER NOT NULL,
    position INTEGER,                -- optional ordinal within divider
    confidence TEXT NOT NULL,        -- 'robot_placed' | 'manually_edited' | 'resort_moved' | 'stale'
    notes TEXT,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES scan_history(id)
);

CREATE TABLE IF NOT EXISTS storage_sessions (
    session_id INTEGER PRIMARY KEY,
    box_id TEXT NOT NULL,
    starting_divider INTEGER NOT NULL,
    ending_divider INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES sessions(id)
);

CREATE TABLE IF NOT EXISTS boxes (
    box_id TEXT PRIMARY KEY,
    label TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_manifests (
    target TEXT NOT NULL,            -- 'moxfield'
    oracle_id TEXT NOT NULL,
    qty INTEGER,
    foil_qty INTEGER,
    condition TEXT,
    last_uploaded_at TEXT,
    PRIMARY KEY (target, oracle_id)
);
```

---

## Preset store contract

Used by the unified sort preset UI. Wraps existing `sort_configs/*.txt` files AND new JSON presets.

```python
# preset_store.py
from dataclasses import dataclass

@dataclass
class PresetBin:
    bin: int
    query: str
    description: Optional[str]

@dataclass
class Preset:
    id: str
    name: str
    builtin: bool
    bins: list[PresetBin]
    fallback_bin: int
    source_path: Optional[str]  # where it came from
    # Metadata for tracking origin (e.g., "Moxfield: <deck>"):
    origin: Optional[str]
    origin_metadata: Optional[dict]


class PresetStore:
    def list(self) -> list[Preset]: ...
    def get(self, preset_id: str) -> Preset: ...
    def save(self, preset: Preset) -> str: ...  # returns id
    def delete(self, preset_id: str) -> None: ...
    def import_legacy(self) -> int:
        """Auto-import sort_configs/*.txt as read-only presets. Returns count imported."""
```

Implementation reads the legacy text format on startup and holds them in-memory as builtin presets alongside any JSON-saved user presets.
