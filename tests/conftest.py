# tests/conftest.py
# ---------------------------------------------------------------------------
# Shared pytest fixtures. Isolation defaults:
#   - every test gets a clean temp enrichment DB on demand
#   - env vars with live-service credentials are stripped automatically
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def tmp_enrichment_db():
    """Temporary enrichment.db with schema created but no data."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="enrichment_test_")
    os.close(fd)
    import enrichment_db
    conn = enrichment_db.get_connection(db_path=path)
    try:
        yield conn
    finally:
        try:
            conn.close()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


@pytest.fixture
def tmp_collection_db():
    """Temporary collection.db."""
    fd, path = tempfile.mkstemp(suffix=".db", prefix="collection_test_")
    os.close(fd)
    import collection_db
    conn = collection_db.get_connection(db_path=path)
    try:
        yield conn
    finally:
        try:
            conn.close()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


@pytest.fixture
def fixtures_dir():
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def probe_snapshots_dir():
    return Path(__file__).parent / "probe_snapshots"


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    """Reset env vars that could point tests at live services."""
    for key in (
        "MOXFIELD_EMAIL",
        "MOXFIELD_PASSWORD",
        "DISCORD_WEBHOOK_URL",
        "NTFY_TOPIC",
        "TCGPLAYER_CLIENT_ID",
        "TCGPLAYER_CLIENT_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)
