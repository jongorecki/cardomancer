"""
Multi-camera registry (TASK-027): role-named cameras, persisted settings,
per-camera API routes, and auto-pause only for the card-ID camera.

No real camera is opened; VideoCapture objects are mocks.
"""

import json
import unittest.mock as mock

import cv2
import pytest

import web_camera
from web_camera import CameraManager


@pytest.fixture
def registry(monkeypatch, tmp_path):
    """Isolated registry: fresh config dict, fresh managers, tmp config file."""
    cfg = web_camera.load_camera_config(str(tmp_path / 'none.json'))
    monkeypatch.setattr(web_camera, '_config', cfg)
    cams = {role: CameraManager(role=role, **web_camera._manager_kwargs(s))
            for role, s in cfg['cameras'].items()}
    monkeypatch.setattr(web_camera, 'cameras', cams)
    monkeypatch.setattr(web_camera, 'CAMERA_CONFIG_PATH',
                        str(tmp_path / 'camera_config.json'))
    return cams


# --- config loading -----------------------------------------------------

def test_defaults_have_down_enabled_and_up_disabled(tmp_path):
    cfg = web_camera.load_camera_config(str(tmp_path / 'missing.json'))
    assert cfg['id_role'] == 'down'
    assert cfg['cameras']['down']['enabled'] is True
    assert cfg['cameras']['up']['enabled'] is False


def test_file_and_env_overrides(tmp_path, monkeypatch):
    path = tmp_path / 'camera_config.json'
    path.write_text(json.dumps({
        'id_role': 'up',
        'cameras': {'up': {'enabled': True, 'device_index': 3}},
    }))
    monkeypatch.setenv('CARDOMANCER_CAM_UP_INDEX', '5')
    monkeypatch.setenv('CARDOMANCER_CAM_DOWN_ENABLED', 'false')
    cfg = web_camera.load_camera_config(str(path))
    assert cfg['id_role'] == 'up'
    assert cfg['cameras']['up']['enabled'] is True
    assert cfg['cameras']['up']['device_index'] == 5       # env wins
    assert cfg['cameras']['up']['fps'] == 60               # default kept
    assert cfg['cameras']['down']['enabled'] is False


def test_unknown_id_role_falls_back_to_down(tmp_path, monkeypatch):
    monkeypatch.setenv('CARDOMANCER_ID_CAMERA', 'sideways')
    cfg = web_camera.load_camera_config(str(tmp_path / 'missing.json'))
    assert cfg['id_role'] == 'down'


# --- registry lookups ---------------------------------------------------

def test_get_camera_by_role_and_id_alias(registry):
    assert web_camera.get_camera('up') is registry['up']
    assert web_camera.get_camera('id') is registry['down']
    web_camera._config['id_role'] = 'up'
    assert web_camera.id_camera() is registry['up']
    with pytest.raises(KeyError):
        web_camera.get_camera('nope')


def test_disabled_camera_does_not_start(registry):
    up = registry['up']
    with mock.patch.object(up, '_open_capture') as open_capture:
        assert up.start() is False
    open_capture.assert_not_called()
    assert 'disabled' in up.get_status()['last_error']


# --- settings persistence + controls -------------------------------------

def test_save_settings_persists_and_applies(registry):
    web_camera.save_camera_settings(
        'up', {'enabled': True, 'rotate': 180, 'flip': 'h'})
    saved = json.loads(open(web_camera.CAMERA_CONFIG_PATH).read())
    assert saved['cameras']['up']['rotate'] == 180
    up = registry['up']
    assert up.enabled is True
    assert up.rotate == cv2.ROTATE_180
    assert up.flip == 1


@pytest.mark.parametrize('bad', [{'rotate': 45}, {'flip': 'x'},
                                 {'colour': 'red'},
                                 {'controls': {'zoom': 2}}])
def test_save_settings_rejects_bad_values_without_writing(registry, bad):
    with pytest.raises(ValueError):
        web_camera.save_camera_settings('up', bad)
    assert not (web_camera.os.path.exists(web_camera.CAMERA_CONFIG_PATH))


def test_controls_applied_in_order_on_open_and_after_update():
    cam = CameraManager(role='up', controls={'exposure': -6,
                                             'auto_exposure': 0.25})
    cap = mock.MagicMock()
    cap.set.return_value = True
    cam._apply_controls(cap)
    props = [c.args[0] for c in cap.set.call_args_list]
    # auto_exposure must be switched to manual before exposure is set
    assert props == [cv2.CAP_PROP_AUTO_EXPOSURE, cv2.CAP_PROP_EXPOSURE]

    cam._cap = cap
    cap.isOpened.return_value = True
    cap.set.reset_mock()
    cam.set_controls(focus=40)
    assert mock.call(cv2.CAP_PROP_FOCUS, 40.0) in cap.set.call_args_list


def test_focus_lock_survives_reconnect():
    cam = CameraManager(role='up')
    cam._focus_locked = True
    cap = mock.MagicMock()
    cam._apply_controls(cap)
    cap.set.assert_any_call(cv2.CAP_PROP_AUTOFOCUS, 0.0)


# --- API routes -----------------------------------------------------------

@pytest.fixture
def client(registry):
    import web_server
    return web_server.app.test_client()


XHR = {'X-Requested-With': 'XMLHttpRequest'}


def test_status_route_selects_camera_by_role(client):
    assert client.get('/api/camera/status').get_json()['role'] == 'down'
    assert client.get('/api/camera/status?cam=up').get_json()['role'] == 'up'
    assert client.get('/api/camera/status?cam=nope').status_code == 404


def test_cameras_route_lists_all(client):
    body = client.get('/api/cameras').get_json()
    assert body['id_role'] == 'down'
    assert set(body['cameras']) == {'down', 'up'}


def test_settings_route_validates(client):
    ok = client.post('/api/cameras/up', json={'rotate': 90}, headers=XHR)
    assert ok.status_code == 200
    assert ok.get_json()['settings']['rotate'] == 90
    bad = client.post('/api/cameras/up', json={'rotate': 45}, headers=XHR)
    assert bad.status_code == 400
    missing = client.post('/api/cameras/side', json={}, headers=XHR)
    assert missing.status_code == 404


# --- auto-pause only for the ID camera -------------------------------------

def test_only_id_camera_health_arms_auto_pause(registry, monkeypatch):
    import web_server
    monkeypatch.setattr(type(web_server.worker), 'state',
                        property(lambda self: 'sorting'), raising=False)
    timers = []

    class FakeTimer:
        def __init__(self, delay, fn, args=()):
            timers.append((delay, fn, args))
            self.daemon = False

        def start(self):
            pass

        def cancel(self):
            pass

    monkeypatch.setattr('threading.Timer', FakeTimer)
    monkeypatch.setattr(web_server.socketio, 'emit', lambda *a, **k: None)
    web_server._install_camera_health_listeners(grace_seconds=0.01)

    registry['up']._set_health('ok')
    registry['up']._set_health('dead')          # not the ID camera
    assert timers == []
    registry['down']._set_health('ok')
    registry['down']._set_health('dead')        # ID camera
    assert len(timers) == 1
