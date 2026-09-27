"""
sort_cycle switch (TASK-097.01): staging vs upcam, selectable without code
changes, fixed for a session, and the up-camera cycle stops safely until
it's built. See docs/design/up_camera.md#reversibility.
"""

import json
import sys
import unittest.mock as mock

import pytest

import machine_settings


@pytest.fixture
def settings_path(monkeypatch, tmp_path):
    path = str(tmp_path / 'machine_settings.json')
    monkeypatch.setattr(machine_settings, 'SETTINGS_PATH', path)
    monkeypatch.delenv('CARDOMANCER_SORT_CYCLE', raising=False)
    return path


# --- machine_settings ------------------------------------------------------

def test_default_is_staging(settings_path):
    assert machine_settings.get_sort_cycle() == 'staging'


def test_set_persists_and_env_overrides(settings_path, monkeypatch):
    machine_settings.set_sort_cycle('upcam')
    assert json.load(open(settings_path))['sort_cycle'] == 'upcam'
    assert machine_settings.get_sort_cycle() == 'upcam'
    monkeypatch.setenv('CARDOMANCER_SORT_CYCLE', 'staging')
    assert machine_settings.get_sort_cycle() == 'staging'


def test_invalid_values(settings_path, monkeypatch):
    with pytest.raises(ValueError):
        machine_settings.set_sort_cycle('conveyor')
    monkeypatch.setenv('CARDOMANCER_SORT_CYCLE', 'conveyor')
    assert machine_settings.get_sort_cycle() == 'staging'


# --- worker dispatch ---------------------------------------------------------

def test_upcam_cycle_pauses_without_motion():
    import web_worker
    gc = sys.modules['gcode_control']
    w = web_worker.SortWorker()
    w._state = 'sorting'
    w.sort_cycle = 'upcam'
    w.continuous_sorting = True
    events = []
    w.set_emit(lambda ev, data: events.append((ev, data)))
    with mock.patch.object(gc, 'move_x') as move_x, \
         mock.patch.object(gc, 'move_z') as move_z, \
         mock.patch.object(gc, '_send_and_wait', create=True) as send:
        w._cmd_detect_and_sort()
    assert w.state == 'paused'
    assert w.continuous_sorting is False
    move_x.assert_not_called()
    move_z.assert_not_called()
    send.assert_not_called()
    assert any(ev == 'session_paused' for ev, _ in events)


def test_new_worker_defaults_to_staging():
    import web_worker
    assert web_worker.SortWorker().sort_cycle == 'staging'


# --- API -----------------------------------------------------------------------

XHR = {'X-Requested-With': 'XMLHttpRequest'}


@pytest.fixture
def client(settings_path, monkeypatch, tmp_path):
    import web_camera
    import web_server
    cfg = web_camera.load_camera_config(str(tmp_path / 'none.json'))
    monkeypatch.setattr(web_camera, '_config', cfg)
    monkeypatch.setattr(web_camera, 'CAMERA_CONFIG_PATH',
                        str(tmp_path / 'camera_config.json'))
    return web_server.app.test_client()


def _set_worker_state(monkeypatch, state):
    import web_server
    monkeypatch.setattr(type(web_server.worker), 'state',
                        property(lambda self: state), raising=False)


def test_sort_cycle_api_get_set_and_validate(client, monkeypatch):
    _set_worker_state(monkeypatch, 'idle')
    assert client.get('/api/machine/sort-cycle').get_json()['sort_cycle'] == 'staging'
    r = client.post('/api/machine/sort-cycle', json={'sort_cycle': 'upcam'}, headers=XHR)
    assert r.status_code == 200 and r.get_json()['sort_cycle'] == 'upcam'
    bad = client.post('/api/machine/sort-cycle', json={'sort_cycle': 'x'}, headers=XHR)
    assert bad.status_code == 400


@pytest.mark.parametrize('state', ['sorting', 'paused'])
def test_cannot_change_cycle_or_id_camera_mid_session(client, monkeypatch, state):
    _set_worker_state(monkeypatch, state)
    r = client.post('/api/machine/sort-cycle', json={'sort_cycle': 'upcam'}, headers=XHR)
    assert r.status_code == 409
    r = client.post('/api/camera/id-role', json={'role': 'up'}, headers=XHR)
    assert r.status_code == 409


def test_id_role_api(client, monkeypatch):
    import web_camera
    _set_worker_state(monkeypatch, 'idle')
    r = client.post('/api/camera/id-role', json={'role': 'up'}, headers=XHR)
    assert r.status_code == 200 and web_camera.id_role() == 'up'
    assert client.post('/api/camera/id-role', json={'role': 'side'},
                       headers=XHR).status_code == 400


def test_session_start_refuses_upcam_without_up_id_camera(client, monkeypatch):
    import web_server
    import gcode_control
    _set_worker_state(monkeypatch, 'idle')
    machine_settings.set_sort_cycle('upcam')
    monkeypatch.setattr(web_server, '_ensure_hardware_ready', lambda: None)
    monkeypatch.setattr(gcode_control, 'get_bin_locations',
                        lambda: {0: 50.0, 1: 150.0})
    r = client.post('/api/session/start', json={}, headers=XHR)
    assert r.status_code == 409
    assert r.get_json()['error'] == 'sort_cycle_camera_mismatch'
