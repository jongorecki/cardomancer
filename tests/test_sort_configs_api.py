# tests/test_sort_configs_api.py
# ---------------------------------------------------------------------------
# Tests for the Phase 0.1 unified sort-preset CRUD endpoints.
#
#   GET    /api/sort/configs                        -> list + metadata
#   GET    /api/sort/configs/<name>                 -> file content + metadata
#   POST   /api/sort/configs/<name>                 -> create/overwrite user preset
#   DELETE /api/sort/configs/<name>                 -> remove user preset
#   POST   /api/sort/configs/<name>/duplicate       -> copy to a new filename
#   POST   /api/sort/validate-query                 -> parser round-trip
#
# Every test runs the web_server Flask app against a throwaway
# SORT_CONFIGS_DIR so we never touch the shipped presets on disk.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import shutil
import tempfile

import pytest


@pytest.fixture
def preset_client(monkeypatch):
    """web_server.app test client with a sandboxed sort_configs/ dir.

    Seeded with:
      - color.txt   (built-in — forwarded from the real repo so the
                    BUILTIN_SORT_PRESETS gate has a real file to reject
                    writes/deletes against)
      - my_user.txt (user preset, a simple valid 3-bin config)
    """
    tmp = tempfile.mkdtemp(prefix='sort_cfg_test_')

    # Seed with a built-in by copying the real color.txt. If it's missing
    # for any reason (dev sandbox), synthesize one.
    import config as _config
    real_builtin = os.path.join(_config.SORT_CONFIGS_DIR, 'color.txt')
    if os.path.exists(real_builtin):
        shutil.copy(real_builtin, os.path.join(tmp, 'color.txt'))
    else:
        with open(os.path.join(tmp, 'color.txt'), 'w', encoding='utf-8') as f:
            f.write('# Color sort (built-in)\nbins: 2\nfallback: 2\n'
                    'bin1: c:w\n')

    # Seed a user preset. Leading "# My user preset" becomes its description.
    with open(os.path.join(tmp, 'my_user.txt'), 'w', encoding='utf-8') as f:
        f.write('# My user preset\nbins: 3\nfallback: 3\nbin1: c:r\n'
                'bin2: c:u\n')

    monkeypatch.setattr(_config, 'SORT_CONFIGS_DIR', tmp, raising=True)
    import web_server
    monkeypatch.setattr(web_server, 'SORT_CONFIGS_DIR', tmp, raising=True)

    client = web_server.app.test_client()
    try:
        yield client, tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# GET /api/sort/configs — list
# ---------------------------------------------------------------------------

def test_list_returns_both_builtins_and_user_presets(preset_client):
    client, _tmp = preset_client
    r = client.get('/api/sort/configs')
    assert r.status_code == 200
    body = r.get_json()
    names = {c['filename'] for c in body['configs']}
    assert 'color.txt' in names
    assert 'my_user.txt' in names
    assert 'color.txt' in body['builtins']


def test_list_includes_description_and_builtin_flag(preset_client):
    client, _tmp = preset_client
    r = client.get('/api/sort/configs')
    body = r.get_json()
    by_name = {c['filename']: c for c in body['configs']}
    assert by_name['my_user.txt']['builtin'] is False
    assert by_name['my_user.txt']['description'] == 'My user preset'
    assert by_name['my_user.txt']['bin_count'] == 3
    # color.txt must be flagged as built-in regardless of whether we
    # seeded it from the real file or synthesized it.
    assert by_name['color.txt']['builtin'] is True


def test_list_ignores_non_txt_files(preset_client):
    client, tmp = preset_client
    with open(os.path.join(tmp, 'README.md'), 'w') as f:
        f.write('not a preset')
    r = client.get('/api/sort/configs')
    names = {c['filename'] for c in r.get_json()['configs']}
    assert 'README.md' not in names


# ---------------------------------------------------------------------------
# GET /api/sort/configs/<name>
# ---------------------------------------------------------------------------

def test_get_returns_content_and_metadata(preset_client):
    client, _tmp = preset_client
    r = client.get('/api/sort/configs/my_user.txt')
    assert r.status_code == 200
    body = r.get_json()
    assert body['filename'] == 'my_user.txt'
    assert 'bin1: c:r' in body['content']
    assert body['bin_count'] == 3
    assert body['builtin'] is False


def test_get_missing_returns_404(preset_client):
    client, _tmp = preset_client
    r = client.get('/api/sort/configs/does_not_exist.txt')
    assert r.status_code == 404


def test_get_rejects_path_traversal(preset_client):
    client, _tmp = preset_client
    # ".." is blocked unconditionally. We don't care whether Werkzeug
    # routes it at all — only that it never returns somebody else's file.
    for bad in ('..', '../foo', 'subdir/foo', '.hidden', ''):
        r = client.get(f'/api/sort/configs/{bad}')
        # Either 400 from our sanitizer or 404 from the router — both
        # mean "no traversal happened". The critical constraint is that
        # we never return 200 with foreign content.
        assert r.status_code != 200, f'traversal leaked for {bad!r}'


# ---------------------------------------------------------------------------
# POST /api/sort/configs/<name> — save
# ---------------------------------------------------------------------------

def test_save_new_user_preset(preset_client):
    client, tmp = preset_client
    content = '# Saved by test\nbins: 2\nfallback: 2\nbin1: c:g\n'
    r = client.post('/api/sort/configs/test_new.txt',
                    json={'content': content})
    assert r.status_code == 200
    body = r.get_json()
    assert body['saved'] is True and body['filename'] == 'test_new.txt'
    assert os.path.exists(os.path.join(tmp, 'test_new.txt'))


def test_save_appends_txt_extension(preset_client):
    client, tmp = preset_client
    content = '# noext\nbins: 1\nfallback: 1\nbin1: c:w\n'
    r = client.post('/api/sort/configs/noext',
                    json={'content': content})
    assert r.status_code == 200
    assert os.path.exists(os.path.join(tmp, 'noext.txt'))


def test_save_rejects_overwriting_builtin(preset_client):
    client, _tmp = preset_client
    r = client.post(
        '/api/sort/configs/color.txt',
        json={'content': '# hijack\nbins: 1\nfallback: 1\nbin1: c:w\n'},
    )
    assert r.status_code == 403
    assert 'read-only' in r.get_json()['error']


def test_save_rejects_empty_content(preset_client):
    client, _tmp = preset_client
    r = client.post('/api/sort/configs/blank.txt', json={'content': '   \n'})
    assert r.status_code == 400


def test_save_rejects_non_parseable_content(preset_client):
    client, _tmp = preset_client
    r = client.post('/api/sort/configs/busted.txt',
                    json={'content': 'this is not a preset'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['error'] == 'invalid_content'


def test_save_rejects_path_traversal(preset_client):
    client, _tmp = preset_client
    r = client.post(
        '/api/sort/configs/..%2Fconfig.py',
        json={'content': '# evil\nbins: 1\nfallback: 1\n'},
    )
    assert r.status_code in (400, 404)


# ---------------------------------------------------------------------------
# DELETE /api/sort/configs/<name>
# ---------------------------------------------------------------------------

def test_delete_user_preset(preset_client):
    client, tmp = preset_client
    r = client.delete('/api/sort/configs/my_user.txt')
    assert r.status_code == 200
    assert not os.path.exists(os.path.join(tmp, 'my_user.txt'))


def test_delete_rejects_builtin(preset_client):
    client, tmp = preset_client
    r = client.delete('/api/sort/configs/color.txt')
    assert r.status_code == 403
    assert os.path.exists(os.path.join(tmp, 'color.txt'))


def test_delete_missing_returns_404(preset_client):
    client, _tmp = preset_client
    r = client.delete('/api/sort/configs/ghost.txt')
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/sort/configs/<name>/duplicate
# ---------------------------------------------------------------------------

def test_duplicate_creates_copy_with_new_name(preset_client):
    client, tmp = preset_client
    r = client.post(
        '/api/sort/configs/my_user.txt/duplicate',
        json={'target': 'my_user_copy'},
    )
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['duplicated'] is True
    assert body['filename'] == 'my_user_copy.txt'
    src = open(os.path.join(tmp, 'my_user.txt'), encoding='utf-8').read()
    dst = open(os.path.join(tmp, 'my_user_copy.txt'), encoding='utf-8').read()
    assert src == dst


def test_duplicate_builtin_allowed(preset_client):
    # Built-ins are read-only but duplicable — that's the whole point of
    # the button for users who want to tweak the color sort.
    client, tmp = preset_client
    r = client.post(
        '/api/sort/configs/color.txt/duplicate',
        json={'target': 'my_color.txt'},
    )
    assert r.status_code == 200
    assert os.path.exists(os.path.join(tmp, 'my_color.txt'))


def test_duplicate_rejects_existing_target(preset_client):
    client, _tmp = preset_client
    r = client.post(
        '/api/sort/configs/color.txt/duplicate',
        json={'target': 'my_user.txt'},
    )
    assert r.status_code == 409
    assert r.get_json()['error'] == 'target_exists'


def test_duplicate_rejects_same_target(preset_client):
    client, _tmp = preset_client
    r = client.post(
        '/api/sort/configs/my_user.txt/duplicate',
        json={'target': 'my_user.txt'},
    )
    assert r.status_code == 400


def test_duplicate_missing_source_returns_404(preset_client):
    client, _tmp = preset_client
    r = client.post(
        '/api/sort/configs/nope.txt/duplicate',
        json={'target': 'anything.txt'},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /api/sort/validate-query
# ---------------------------------------------------------------------------

def test_validate_query_valid(preset_client):
    client, _tmp = preset_client
    r = client.post('/api/sort/validate-query', json={'query': 'c:w t:creature'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['valid'] is True


def test_validate_query_invalid(preset_client):
    client, _tmp = preset_client
    # Unclosed paren is a parse error the parser reliably rejects.
    r = client.post('/api/sort/validate-query', json={'query': '(c:w'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['valid'] is False
    assert body.get('error')
