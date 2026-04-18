# Test Framework

Existing test style: `unittest.TestCase` subclasses with `tempfile`-based fixtures, runnable under pytest. Match that. Add pytest-specific features (fixtures, markers) only where useful.

---

## Layout

```
tests/
├── __init__.py                          (existing)
├── test_cards.py                        (existing)
├── test_collection_db.py                (existing)
├── test_detection.py                    (existing — don't modify)
├── test_hashing.py                      (existing — don't modify)
├── test_query_parser.py                 (existing — extend for new tokens)
├── test_sort_config.py                  (existing)
├── conftest.py                          NEW — shared fixtures
├── enrichment/                          NEW
│   ├── __init__.py
│   ├── test_enrichment_db.py
│   ├── test_enrichment_repo.py
│   ├── test_tagger.py
│   ├── test_edhrec.py
│   ├── test_edhtop16.py
│   ├── test_spellbook.py
│   ├── test_buylist_ck.py
│   ├── test_cross_source_sanity.py
│   └── test_scheduler.py
├── integrations/                        NEW
│   ├── __init__.py
│   ├── test_moxfield_deck.py
│   ├── test_moxfield_binder.py
│   └── test_preset_store.py
├── locator/                             NEW
│   ├── __init__.py
│   ├── test_locator.py
│   └── test_storage.py
├── probes/                              NEW
│   ├── __init__.py
│   └── test_all_probes.py               (invokes probes/run_all.py)
├── fixtures/                            NEW
│   ├── tagger/
│   │   ├── tag_list_response.json
│   │   └── tag_detail_removal.json
│   ├── edhrec/
│   │   ├── commander_atraxa.json
│   │   └── card_sol_ring.json
│   ├── edhtop16/
│   │   ├── tournaments_2025_q4.json
│   │   └── decklist_response.json
│   ├── spellbook/
│   │   └── variants_page1.json
│   ├── moxfield/
│   │   └── deck_sample.json
│   └── buylist_ck/
│       └── buylist_page.html
└── probe_snapshots/                     NEW
    ├── scryfall_bulk_pinned.json
    ├── scryfall_search_pinned.json
    ├── tagger_pinned.json
    ├── edhrec_commander_pinned.json
    ├── edhrec_card_pinned.json
    ├── edhtop16_pinned.json
    ├── spellbook_pinned.json
    └── moxfield_deck_pinned.json
```

---

## conftest.py (shared fixtures)

```python
# tests/conftest.py
import os
import tempfile
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def tmp_enrichment_db():
    """Temporary enrichment.db with schema created but no data."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import enrichment_db
    conn = enrichment_db.get_connection(db_path=path)
    yield conn
    conn.close()
    os.unlink(path)


@pytest.fixture
def tmp_collection_db():
    """Temporary collection.db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import collection_db
    conn = collection_db.get_connection(db_path=path)
    yield conn
    conn.close()
    os.unlink(path)


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def probe_snapshots_dir():
    return Path(__file__).parent / "probe_snapshots"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Reset env vars to test defaults; prevent accidental live-service calls."""
    monkeypatch.delenv("MOXFIELD_EMAIL", raising=False)
    monkeypatch.delenv("MOXFIELD_PASSWORD", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)


@pytest.fixture
def mock_http(monkeypatch):
    """httpx/gql responses from fixtures — never hit live services during tests."""
    import httpx

    class _MockClient:
        def __init__(self, responses: dict[str, httpx.Response]):
            self._responses = responses

        def get(self, url, **kw):
            for prefix, resp in self._responses.items():
                if url.startswith(prefix):
                    return resp
            raise AssertionError(f"Unmocked URL: {url}")

        def post(self, url, **kw):
            return self.get(url, **kw)

    return _MockClient
```

---

## Test patterns

### Unit test (matches existing style)

```python
# tests/enrichment/test_enrichment_db.py
import unittest
import tempfile, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import enrichment_db


class TestEnrichmentDB(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmpdir, "test_enrichment.db")
        self.conn = enrichment_db.get_connection(db_path=self.db_path)

    def tearDown(self):
        self.conn.close()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_tags_table_exists(self):
        cursor = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tags'"
        )
        self.assertIsNotNone(cursor.fetchone())

    def test_insert_tag(self):
        oid = "abc-123"
        self.conn.execute(
            "INSERT INTO tags (oracle_id, tag_name, source) VALUES (?, ?, ?)",
            (oid, "removal", "scryfall_search")
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM tags WHERE oracle_id=?", (oid,)
        ).fetchone()
        self.assertEqual(row["tag_name"], "removal")
```

### Fixture-based source test

```python
# tests/enrichment/test_tagger.py
import json
import pytest
from web_enrichment.tagger import TaggerSource


def test_parse_tag_list(fixtures_dir):
    data = json.loads((fixtures_dir / "tagger" / "tag_list_response.json").read_text())
    source = TaggerSource()
    catalog = source._parse_tag_catalog(data)
    assert len(catalog) > 0
    assert any(t.name == "removal" for t in catalog)


def test_refresh_empty_response_noop(tmp_enrichment_db, mock_http, monkeypatch):
    # Mock empty response; verify no rows written and no existing rows wiped
    tmp_enrichment_db.execute(
        "INSERT INTO tags (oracle_id, tag_name, source) VALUES (?, ?, ?)",
        ("existing-id", "existing-tag", "scryfall_search")
    )
    tmp_enrichment_db.commit()

    # ... configure TaggerSource with mocked client returning empty ...
    # source.refresh(emit=None)

    row = tmp_enrichment_db.execute(
        "SELECT * FROM tags WHERE oracle_id='existing-id'"
    ).fetchone()
    assert row is not None  # existing data preserved
```

### Cross-source sanity test

```python
# tests/enrichment/test_cross_source_sanity.py
import pytest

# Known-staple assertions; run against a populated enrichment.db
# (setup populates from fixtures, not live sources)

def test_sol_ring_is_universal_staple(populated_enrichment_db):
    repo = EnrichmentRepo(populated_enrichment_db)
    sol_ring = repo.get_card("<sol-ring-oracle-id>")
    assert sol_ring is not None
    assert sol_ring.staples["universal"] is True
    assert sol_ring.staples["cedh"] is True
    assert sol_ring.salt > 0
    assert len(sol_ring.combos) > 0  # Sol Ring + Basalt Monolith etc.


def test_grizzly_bears_is_vanilla_cull(populated_enrichment_db):
    repo = EnrichmentRepo(populated_enrichment_db)
    bears = repo.get_card("<grizzly-bears-oracle-id>")
    assert "vanilla" in bears.tags
    assert not any(bears.staples.values())


def test_thassas_oracle_in_cedh_and_combos(populated_enrichment_db):
    repo = EnrichmentRepo(populated_enrichment_db)
    oracle = repo.get_card("<thassas-oracle-oracle-id>")
    assert oracle.staples["cedh"] is True
    assert len(oracle.combos) >= 1
```

### Probe test

```python
# tests/probes/test_all_probes.py
import subprocess, sys
from pathlib import Path

PROBES_DIR = Path(__file__).parent.parent.parent / "probes"

def test_probe_script_exists_for_every_source():
    expected = {"scryfall_bulk", "scryfall_search", "tagger", "edhrec",
                "edhtop16", "spellbook", "moxfield", "buylist_ck"}
    found = {p.stem.replace("probe_", "") for p in PROBES_DIR.glob("probe_*.py")}
    missing = expected - found
    assert not missing, f"Missing probe scripts: {missing}"


@pytest.mark.live  # requires network; skipped by default
def test_all_probes_pass_live():
    result = subprocess.run(
        [sys.executable, str(PROBES_DIR / "run_all.py")],
        capture_output=True
    )
    assert result.returncode == 0, result.stdout.decode() + result.stderr.decode()
```

---

## Markers

```ini
# pyproject.toml (or pytest.ini)
[tool.pytest.ini_options]
markers = [
    "live: requires network access to external services (skipped by default)",
    "slow: takes more than 1s to run",
]
testpaths = ["tests"]
addopts = "-m 'not live'"
```

Running tests:
- Default: `pytest tests/` → all non-live tests
- With live: `pytest tests/ -m live` → probes hit real endpoints
- Slow only: `pytest tests/ -m slow`

---

## Coverage expectations

- **New code**: every new function has at least one test.
- **Error paths**: empty response, malformed response, rate limit (mocked 429), network timeout.
- **Idempotency**: running `refresh()` twice produces identical DB state.
- **Shape drift**: every source has a schema-snapshot test that fails on unexpected fields.

No hard coverage percentage requirement. Focus on correctness over line coverage.

---

## Performance tests (optional)

Not required for v1. If added later, put under `tests/perf/` with `@pytest.mark.slow`.

---

## What NOT to test

- Existing detection/hashing pipeline (scope fence)
- Existing motion control (scope fence)
- Trivial getters/setters
- Framework code (Flask routing itself)

---

## Running the full suite

```bash
# Fast path (CI-equivalent)
pytest tests/

# With live network probes
pytest tests/ -m "not slow"  # still skip slow tests
pytest tests/ -m live        # ONLY live tests

# Single file while debugging
pytest tests/enrichment/test_tagger.py -v

# Single test
pytest tests/enrichment/test_tagger.py::TestTaggerSource::test_parse_tag_list -v
```

Expected full-suite runtime (non-live): < 60s.
