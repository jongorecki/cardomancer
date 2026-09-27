# machine_settings.py
# ---------------------------------------------------------------------------
# Machine-level settings that change with the physical build rather than
# per session. Stored in machine_settings.json next to this file
# (machine-specific, gitignored).
#
#   sort_cycle  'staging' — pick, drop on the staging platform, image with
#                           the down camera, re-pick, place (original build)
#               'upcam'   — pick, image the held card with the up camera,
#                           place (docs/design/up_camera.md)
#
# Env override: CARDOMANCER_SORT_CYCLE.
# ---------------------------------------------------------------------------

import json
import logging
import os

logger = logging.getLogger(__name__)

SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'machine_settings.json')

SORT_CYCLES = ('staging', 'upcam')
DEFAULTS = {'sort_cycle': 'staging'}


def load(path=None):
    settings = dict(DEFAULTS)
    path = path or SETTINGS_PATH
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                settings.update(json.load(f))
        except Exception as e:
            logger.error(f"could not read {path}: {e}; using defaults")
    if os.environ.get('CARDOMANCER_SORT_CYCLE'):
        settings['sort_cycle'] = os.environ['CARDOMANCER_SORT_CYCLE']
    if settings['sort_cycle'] not in SORT_CYCLES:
        logger.error(f"unknown sort_cycle {settings['sort_cycle']!r}; "
                     f"using 'staging'")
        settings['sort_cycle'] = 'staging'
    return settings


def get_sort_cycle(path=None):
    return load(path)['sort_cycle']


def set_sort_cycle(cycle, path=None):
    """Validate and persist the sort cycle. Returns the saved value."""
    if cycle not in SORT_CYCLES:
        raise ValueError(f"sort_cycle must be one of {list(SORT_CYCLES)}")
    path = path or SETTINGS_PATH
    saved = {}
    if os.path.exists(path):
        try:
            with open(path, encoding='utf-8') as f:
                saved = json.load(f)
        except Exception:
            saved = {}
    saved['sort_cycle'] = cycle
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(saved, f, indent=2)
    os.replace(tmp, path)
    return cycle
