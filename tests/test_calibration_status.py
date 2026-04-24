# tests/test_calibration_status.py
# ---------------------------------------------------------------------------
# Tests for /api/calibration/status (Phase 4 item 4.22 — calibration wizard).
#
# The endpoint must return a `wizard` dict with the six per-step booleans
# the frontend pre-check consumes:
#   machine_connected, camera_connected, bin_x_calibrated,
#   bin_z_probed, staging_roi_set, focus_locked
#
# Each boolean is driven from live state (serial connected, camera
# active, focus locked) or on-disk artifacts (_last_setup.json,
# staging_roi.json). We stub those deps via monkeypatch and assert the
# resulting JSON is shaped correctly.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def server_app(monkeypatch, tmp_path):
    """Import web_server fresh with an isolated _last_setup.json path."""
    import web_server
    import web_worker
    import web_camera
    import gcode_control

    # Point the worker at a throwaway setup file so tests don't read
    # whatever happens to be on disk in the repo root. `_last_setup_path`
    # is a property on SortWorker — replace the property with a lambda
    # so monkeypatch can restore the real one after the test.
    setup_path = str(tmp_path / '_last_setup.json')
    monkeypatch.setattr(type(web_worker.worker), '_last_setup_path',
                        property(lambda self: setup_path))

    # Point STAGING_ROI_PATH at the tmp path too, so the wizard
    # boolean reflects only what the test creates.
    import config as _config
    monkeypatch.setattr(_config, 'STAGING_ROI_PATH',
                        str(tmp_path / 'staging_roi.json'), raising=False)

    # Default to everything OFF. Individual tests override as needed.
    monkeypatch.setattr(gcode_control, 'is_connected', lambda: False, raising=True)

    # Camera: force is_active False + focus unlocked by patching the
    # underlying _cap and _focus_locked flags. is_active is a @property
    # that checks self._cap and thread state, so just nudge the flag.
    monkeypatch.setattr(web_camera.camera, '_focus_locked', False, raising=False)
    if hasattr(web_camera.camera, '_cap'):
        monkeypatch.setattr(web_camera.camera, '_cap', None, raising=False)

    return web_server.app.test_client()


def _write_last_setup(path, *, dest_bins=(1, 2, 3), probed_bins=None,
                      include_source=True):
    """Write a minimal _last_setup.json so the booleans resolve."""
    locations = {}
    if include_source:
        locations['0'] = 547.1
    for b in dest_bins:
        locations[str(b)] = 100.0 + 50.0 * b
    probe_results = {}
    if probed_bins is None:
        probed_bins = dest_bins
    if include_source:
        probe_results['0'] = 38.0
    for b in probed_bins:
        probe_results[str(b)] = 2.0
    payload = {
        'version': 1,
        'saved_at': 0,
        'locations': locations,
        'probe_results': probe_results,
    }
    with open(path, 'w') as f:
        json.dump(payload, f)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_status_endpoint_exists(server_app):
    resp = server_app.get('/api/calibration/status')
    assert resp.status_code == 200
    body = resp.get_json()
    assert 'wizard' in body
    wiz = body['wizard']
    for key in ('machine_connected', 'camera_connected', 'bin_x_calibrated',
                'bin_z_probed', 'staging_roi_set', 'focus_locked'):
        assert key in wiz, f'missing key {key}'


def test_all_false_on_fresh_state(server_app):
    """Baseline: nothing connected, no artifacts on disk."""
    resp = server_app.get('/api/calibration/status')
    wiz = resp.get_json()['wizard']
    assert wiz['machine_connected'] is False
    assert wiz['camera_connected'] is False
    assert wiz['bin_x_calibrated'] is False
    assert wiz['bin_z_probed'] is False
    assert wiz['staging_roi_set'] is False
    assert wiz['focus_locked'] is False


def test_machine_connected_true_when_gcode_reports_connected(
        server_app, monkeypatch):
    import gcode_control
    monkeypatch.setattr(gcode_control, 'is_connected', lambda: True)
    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert wiz['machine_connected'] is True


def test_camera_connected_true_when_camera_active(server_app, monkeypatch):
    import web_camera
    monkeypatch.setattr(type(web_camera.camera), 'is_active',
                        property(lambda self: True))
    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert wiz['camera_connected'] is True


def test_bin_x_calibrated_reads_last_setup(server_app, tmp_path):
    import web_worker
    path = web_worker.worker._last_setup_path
    _write_last_setup(path, dest_bins=(1, 2, 3), probed_bins=())
    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert wiz['bin_x_calibrated'] is True
    # No probe entries for dest bins -> Z probe still False.
    assert wiz['bin_z_probed'] is False


def test_bin_z_probed_requires_every_dest_probed(server_app, tmp_path):
    import web_worker
    path = web_worker.worker._last_setup_path
    # 3 dest bins, only 2 probed -> not complete.
    _write_last_setup(path, dest_bins=(1, 2, 3), probed_bins=(1, 2))
    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert wiz['bin_x_calibrated'] is True
    assert wiz['bin_z_probed'] is False

    # Now all 3 probed -> complete.
    _write_last_setup(path, dest_bins=(1, 2, 3), probed_bins=(1, 2, 3))
    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert wiz['bin_z_probed'] is True


def test_staging_roi_set_requires_file(server_app, tmp_path):
    import config as _config
    roi_path = _config.STAGING_ROI_PATH
    assert not os.path.exists(roi_path)
    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert wiz['staging_roi_set'] is False

    Path(roi_path).write_text('[[0,0],[1,0],[1,1],[0,1]]')
    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert wiz['staging_roi_set'] is True


def test_focus_locked_reflects_camera_flag(server_app, monkeypatch):
    import web_camera
    monkeypatch.setattr(web_camera.camera, '_focus_locked', True, raising=False)
    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert wiz['focus_locked'] is True


def test_flat_keys_mirror_wizard_dict(server_app, monkeypatch):
    """Top-level keys should mirror wizard booleans for simple probes."""
    import gcode_control
    monkeypatch.setattr(gcode_control, 'is_connected', lambda: True)
    body = server_app.get('/api/calibration/status').get_json()
    assert body['machine_connected'] is True
    assert body['wizard']['machine_connected'] is True


def test_all_satisfied_is_happy_path(server_app, monkeypatch, tmp_path):
    """Every step true -> all six booleans true."""
    import gcode_control
    import web_camera
    import web_worker
    import config as _config

    monkeypatch.setattr(gcode_control, 'is_connected', lambda: True)
    monkeypatch.setattr(type(web_camera.camera), 'is_active',
                        property(lambda self: True))
    monkeypatch.setattr(web_camera.camera, '_focus_locked', True, raising=False)
    _write_last_setup(web_worker.worker._last_setup_path,
                      dest_bins=(1, 2, 3, 4),
                      probed_bins=(1, 2, 3, 4))
    Path(_config.STAGING_ROI_PATH).write_text('[[0,0],[1,0],[1,1],[0,1]]')

    wiz = server_app.get('/api/calibration/status').get_json()['wizard']
    assert all(wiz.values()), f'not all satisfied: {wiz}'


# ---------------------------------------------------------------------------
# Smoke test: wizard HTML renders on the index page
# ---------------------------------------------------------------------------

def test_index_html_contains_wizard_modal(server_app):
    resp = server_app.get('/')
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'calibration-wizard-modal' in html
    assert 'Run calibration wizard' in html
    # Step count badge text and rail step labels
    assert 'wizard-step-header' in html
    assert 'wizard-step-rail' in html
    # Wizard JS is loaded
    assert 'calibration_wizard.js' in html
