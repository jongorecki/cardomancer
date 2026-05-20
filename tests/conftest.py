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


@pytest.fixture(autouse=True)
def _enable_flask_testing():
    """Mark the Flask app as TESTING for the lifetime of every test.

    The _csrf_origin_check before_request hook in web_server.py bypasses
    its Origin/Referer check when app.config['TESTING'] is truthy.
    Without this, every client.post() in the integration tests would
    eat a 403 because the test client doesn't set Origin/Referer or
    X-Requested-With. The CSRF middleware has its own dedicated test
    file that toggles TESTING off explicitly to exercise the rejection
    paths.
    """
    # Import lazily so importing web_server's heavy dependencies isn't
    # forced on tests that don't touch the Flask app.
    try:
        import web_server
    except Exception:
        yield
        return
    prev = web_server.app.config.get('TESTING', False)
    web_server.app.config['TESTING'] = True
    try:
        yield
    finally:
        web_server.app.config['TESTING'] = prev
