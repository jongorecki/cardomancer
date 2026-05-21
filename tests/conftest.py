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
import unittest.mock as _mock
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ---------------------------------------------------------------------------
# Hardware-module mock (gcode_control)
# ---------------------------------------------------------------------------
#
# Several test files need to import web_server (and its transitive
# imports — web_worker, web_camera, etc.) without a connected machine.
# The pattern across the suite has been a per-file `_install_hw_mocks()`
# that swaps `sys.modules['gcode_control']` for a MagicMock with the
# attributes web_server reads at import time.
#
# CRITICAL GOTCHA: this function MUST be called from inside `setUpClass`
# (or a per-test setUp), NEVER at module top level. Calling it at module
# top-level fires during pytest's collection phase, BEFORE other test
# modules have imported gcode_control. Those later imports then bind to
# the MagicMock instead of the real module — silently breaking unrelated
# tests (we've hit this bug once already; see commit 9c8b539's notes).
#
# Centralizing the helper here gives us ONE place to add new mock attrs
# when web_server starts reading something new, and lets per-file
# _install_hw_mocks() functions stay thin pass-throughs.

def install_gcode_control_mock() -> None:
    """Replace sys.modules['gcode_control'] with a MagicMock if it's
    not already mocked. **Call inside setUpClass or setUp — never at
    module top level** (see GOTCHA in module docstring).

    Idempotent: subsequent calls are no-ops once a mock is installed.
    """
    existing = sys.modules.get('gcode_control')
    if isinstance(existing, _mock.MagicMock):
        return
    gcode_mock = _mock.MagicMock()
    gcode_mock.is_connected = lambda: False
    gcode_mock.ser = None
    gcode_mock.SERIAL_PORT = 'COM3'
    gcode_mock.X_SOURCE_BIN = 547.1
    gcode_mock.X_STAGING_POSITION = 427.9
    gcode_mock.CAMERA_X_OFFSET = 100.0
    gcode_mock.get_bin_locations = lambda: {0: 547.1, 1: 100.0}
    gcode_mock._last_serial_error = None
    sys.modules['gcode_control'] = gcode_mock


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
