# web_server.py
# ---------------------------------------------------------------------------
# Flask web server for the MTG Card Sorter.
# Provides REST API, SocketIO events, and serves the single-page UI.
# ---------------------------------------------------------------------------

import os
import io
import json
import time
import atexit
import signal
import logging
import logging.handlers
import sys
import traceback

# Load .env into os.environ before anything else reads env vars. Missing
# file is fine — python-dotenv is silent on absent paths.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from flask import Flask, render_template, request, jsonify, Response, send_file
from flask_socketio import SocketIO

from web_camera import camera
from web_worker import worker
from web_motion_sim import motion_tracker, simulator
from web_database import db_updater
from web_calibration import calibrator

import cv2

from config import SCRIPT_DIR, SORT_CONFIGS_DIR, SORTING_MODES, SCAN_LOGS_DIR

import enrichment_db
from web_enrichment.repo import EnrichmentRepo
from web_enrichment.scheduler import RefreshScheduler
from web_enrichment.spellbook import SpellbookSource
from web_enrichment.edhrec import EDHRECSource
from web_enrichment.edhtop16 import EDHTop16Source
from web_enrichment.tagger import TaggerSource
from web_enrichment.scrape_tagger_catalogue import TaggerCatalogueSource
from web_enrichment.buylist_ck import CardKingdomBuylistSource


# ---------------------------------------------------------------------------
# Logging — rotating file + stdout tee for post-mortem debugging
# ---------------------------------------------------------------------------
#
# Recursion guard. The stdout/stderr tee below routes writes through the
# root logger. If a handler errors (e.g. RotatingFileHandler can't rename
# the log file on Windows), Python's default `handleError` writes the
# traceback to sys.stderr — which on this app IS the tee. That puts us
# back into logging, the handler errors again, and we loop until the
# stack blows.
#
# `_tee_recursion.active` flips True while we're already inside a
# tee-driven log call; the write() method respects it and bypasses
# logging on re-entry, so logging failures degrade quietly instead of
# avalanching.
import threading as _threading
_tee_recursion = _threading.local()


class _SafeRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that survives Windows rename failures.

    Windows treats `os.rename(open_file, ...)` as a sharing violation,
    and the same applies if any external process (antivirus scanner,
    `tail -f` in another shell, the editor preview) has a transient
    handle to the file at the rotation moment. The default behaviour
    is to surface the error via `handleError` which writes to
    sys.stderr — and our stderr is the logger tee, so the error
    triggers another rotation attempt, which fails again. Infinite
    loop.

    Subclassing rotate() so we catch the OS error, write a single
    note to the *real* stderr (bypassing the tee via sys.__stderr__),
    and return cleanly. Logging continues; the existing file keeps
    growing past maxBytes until the next rotation attempt succeeds.
    A bloated log file is a better failure mode than a stack-blown
    process.
    """

    def rotate(self, source, dest):
        try:
            super().rotate(source, dest)
        except (PermissionError, OSError) as exc:
            # NEVER route this through logging or sys.stderr — both
            # would re-enter the handler. The real underlying stream
            # is what the operator sees in the launcher window.
            try:
                sys.__stderr__.write(
                    f"[card_sorter] log rotation skipped "
                    f"({type(exc).__name__}: {exc}); "
                    f"file will keep growing until next attempt.\n"
                )
            except Exception:
                pass


def _setup_logging():
    """
    Configure a rotating log file so we can reconstruct what happened
    after a crash. Also tees stdout/stderr into the log so all the
    existing `print(...)` statements end up captured without having to
    rewrite them all to use logging.
    """
    logs_dir = os.path.join(SCRIPT_DIR, 'logs')
    try:
        os.makedirs(logs_dir, exist_ok=True)
    except Exception as e:
        print(f"[server] could not create logs dir: {e}")
        return

    log_path = os.path.join(logs_dir, 'card_sorter.log')
    handler = _SafeRotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8')
    handler.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(message)s'))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Belt-and-suspenders: stop the default handleError → sys.stderr
    # path from ever firing in production. If a handler emit fails, it
    # fails silently rather than dumping a traceback into the tee. The
    # tee's own recursion guard would catch the loop anyway, but
    # turning raiseExceptions off keeps the operator console clean.
    logging.raiseExceptions = False
    # Avoid duplicate handlers on reload (cover both the original
    # RotatingFileHandler class and our subclass).
    root.handlers = [h for h in root.handlers
                     if not isinstance(h, logging.handlers.RotatingFileHandler)]
    root.addHandler(handler)

    # Tee stdout/stderr into the logger so every print() lands in the file.
    # Must be defensive about input type: Click/colorama on Windows
    # occasionally passes `bytes` through the stdout pipeline (it
    # intercepts ANSI codes at the byte level), and we'd crash if we
    # tried `str += bytes`. We also expose `buffer`, `encoding`, and
    # `errors` so libraries that sniff for a binary stream (colorama
    # included) can coexist with the tee.
    _real_stdout = sys.__stdout__
    _real_stderr = sys.__stderr__

    class _StreamToLogger:
        def __init__(self, level, original):
            self._level = level
            self._original = original
            self._buffer = ''

        # Attributes some libraries check for on stdout.
        @property
        def buffer(self):
            # Expose the raw binary buffer if the underlying stream
            # has one (real stdout does). Click/colorama will prefer
            # this for byte writes and bypass our write() entirely.
            return getattr(self._original, 'buffer', None)

        @property
        def encoding(self):
            return getattr(self._original, 'encoding', 'utf-8') or 'utf-8'

        @property
        def errors(self):
            return getattr(self._original, 'errors', 'replace')

        def write(self, msg):
            # Normalize to str. Some callers (colorama on Windows) send
            # bytes through here.
            if isinstance(msg, (bytes, bytearray)):
                try:
                    msg = msg.decode(self.encoding, errors='replace')
                except Exception:
                    msg = msg.decode('utf-8', errors='replace')
            elif not isinstance(msg, str):
                try:
                    msg = str(msg)
                except Exception:
                    return
            try:
                self._original.write(msg)
            except Exception:
                pass
            if not msg:
                return
            # Recursion guard. If we're already inside a log call that
            # came from this tee (because a handler error wrote to
            # sys.stderr, which is also us), don't re-enter logging —
            # just forward to the original stream and stop. This is
            # the lock that breaks the rotate-fail → handleError →
            # stderr → log → rotate-fail loop that used to lock up
            # the kiosk during a database update.
            if getattr(_tee_recursion, 'active', False):
                return
            try:
                _tee_recursion.active = True
                self._buffer += msg
                while '\n' in self._buffer:
                    line, self._buffer = self._buffer.split('\n', 1)
                    if line.strip():
                        logging.log(self._level, line.rstrip())
            except Exception:
                # Never let a logging failure break the write path.
                self._buffer = ''
            finally:
                _tee_recursion.active = False

        def writelines(self, lines):
            for line in lines:
                self.write(line)

        def flush(self):
            try:
                self._original.flush()
            except Exception:
                pass

        def isatty(self):
            try:
                return self._original.isatty()
            except Exception:
                return False

        def fileno(self):
            # Some libraries (e.g. subprocess piping) want a real fd.
            # Delegate to the underlying stream if possible.
            return self._original.fileno()

    sys.stdout = _StreamToLogger(logging.INFO, sys.__stdout__)
    sys.stderr = _StreamToLogger(logging.ERROR, sys.__stderr__)

    # Unhandled exception hook — so we catch things that escape try/except.
    def _excepthook(exc_type, exc_value, exc_tb):
        logging.critical(
            "UNHANDLED EXCEPTION: %s",
            ''.join(traceback.format_exception(exc_type, exc_value, exc_tb)))
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook

    logging.info("=" * 60)
    logging.info("Logging started — log file: %s", log_path)
    logging.info("=" * 60)


_setup_logging()

logger = logging.getLogger(__name__)

app = Flask(__name__)


# ---------------------------------------------------------------------------
# SECRET_KEY — used by Flask sessions and CSRF token signing.
# ---------------------------------------------------------------------------
# Priority order:
#   1. $CARDOMANCER_SECRET_KEY environment variable (ops-controlled).
#   2. ~/.cardomancer_secret_key file generated on first run (per-install
#      persistent random key).
#   3. Falls back to in-process random key if disk write fails (sessions
#      survive within a single process but are invalidated on restart).
#
# Anything is better than the prior hardcoded 'card-sorter-secret' which
# is identical across every install and recoverable from the repo.
def _load_or_create_secret_key():
    env_key = os.environ.get('CARDOMANCER_SECRET_KEY')
    if env_key:
        return env_key
    key_path = os.path.join(os.path.expanduser('~'),
                            '.cardomancer_secret_key')
    try:
        if os.path.isfile(key_path):
            with open(key_path, 'r', encoding='utf-8') as f:
                stored = f.read().strip()
            if stored:
                return stored
        import secrets
        new_key = secrets.token_hex(32)
        try:
            with open(key_path, 'w', encoding='utf-8') as f:
                f.write(new_key)
            # Best-effort chmod 600 so other users on the box can't read.
            try:
                os.chmod(key_path, 0o600)
            except Exception:
                pass
            logger.info("Generated new SECRET_KEY at %s", key_path)
        except Exception:
            logger.warning("Could not persist SECRET_KEY to %s; "
                           "using ephemeral key for this process", key_path)
        return new_key
    except Exception:
        # Last-resort fallback. Sessions/cookies will be invalidated
        # on restart but the app still boots.
        import secrets
        return secrets.token_hex(32)


app.config['SECRET_KEY'] = _load_or_create_secret_key()

# Session cookie hardening. SameSite=Lax (not Strict) so the cookie
# still rides on top-level GET navigations from external links (the
# kiosk operator clicking a bookmark) while blocking the cross-site
# POST that CSRF needs. HTTPOnly so JS can't read the cookie value.
# Secure stays False because Cardomancer ships on LAN HTTP by default;
# operators on HTTPS can flip this via env var.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = (
    os.environ.get('CARDOMANCER_HTTPS') == '1'
)

socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')


# ---------------------------------------------------------------------------
# CSRF protection — Origin/Referer check on state-changing requests.
# ---------------------------------------------------------------------------
# We don't currently have user accounts to attack via cross-site request
# forgery, but the kiosk model means an operator may have other tabs
# open while the Cardomancer UI is also open in the browser. A malicious
# site in another tab could otherwise POST to /api/sort/start or /api/
# estop and Cardomancer would obey. This middleware blocks that.
#
# Strategy: on POST/PUT/DELETE/PATCH, require either:
#   - the request was issued by our own JS (it sets
#     X-Requested-With: XMLHttpRequest, which cross-origin browser
#     requests cannot set without a CORS preflight that we never
#     answer), OR
#   - the Origin or Referer header matches the request host (so a
#     classic form POST from our own UI still works even without the
#     header).
#
# Socket.IO traffic is exempt — it uses a long-lived connection that
# the upstream auth handler already gates.

_CSRF_SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
# Endpoints that legitimately accept cross-origin POSTs and so opt out
# of the Origin check. Keep this list short and audited.
_CSRF_EXEMPT_PATHS = {
    '/socket.io/',  # SocketIO has its own connection-level gating
}


def _is_csrf_exempt(path: str) -> bool:
    """Allow any path prefixed with one of the exempt entries."""
    return any(path.startswith(p) for p in _CSRF_EXEMPT_PATHS)


def _same_origin(header_value: str, host_url: str) -> bool:
    """True if `header_value` (an Origin or Referer URL) targets the
    same scheme + host as `host_url` (Flask's request.host_url)."""
    if not header_value or not host_url:
        return False
    # request.host_url includes the trailing slash and scheme; trim
    # it down to scheme://host[:port] for prefix matching.
    base = host_url.rstrip('/')
    return header_value == base or header_value.startswith(base + '/')


@app.before_request
def _csrf_origin_check():
    """Reject state-changing requests whose Origin/Referer doesn't
    match the kiosk's own host."""
    # Bypass when the Flask test client is driving — pytest fixtures
    # don't set Origin / Referer / X-Requested-With by default, and
    # forcing every test to wire them up would yield no real coverage
    # signal. The TESTING flag is set by the test fixture.
    if app.config.get('TESTING'):
        return None
    method = request.method.upper()
    if method in _CSRF_SAFE_METHODS:
        return None
    if _is_csrf_exempt(request.path):
        return None

    # 1. Custom-header fingerprint: only same-origin JS can set this
    #    (cross-origin browsers need a CORS preflight that we don't
    #    serve, so this header reliably proves the request came from
    #    our own UI). This is the cheap fast path.
    xrw = request.headers.get('X-Requested-With', '')
    if xrw == 'XMLHttpRequest':
        return None

    # 2. Origin / Referer match. Origin is set by browsers on POST
    #    even in modern browsers; Referer is set when the user
    #    navigates from our own page. Both are spoof-resistant by
    #    same-origin policy.
    origin = request.headers.get('Origin', '')
    referer = request.headers.get('Referer', '')
    if _same_origin(origin, request.host_url):
        return None
    if _same_origin(referer, request.host_url):
        return None

    logger.warning(
        "CSRF: rejected %s %s — origin=%r referer=%r xrw=%r",
        method, request.path, origin, referer, xrw,
    )
    return jsonify({
        'error': 'csrf_failed',
        'message': ('This request was blocked because it did not '
                    'come from the Cardomancer UI. Reload the page '
                    'and try again.'),
    }), 403

# Wire up emit callbacks
worker.set_emit(lambda event, data: socketio.emit(event, data))
db_updater.set_emit(lambda event, data: socketio.emit(event, data))
calibrator.set_emit(lambda event, data: socketio.emit(event, data))


# =========================================================================
# Enrichment (Phase 1 — real sources for Spellbook, EDHREC, edhtop16,
# Tagger; stubs retained for buylist_ck and prices which are Phase 3+)
# =========================================================================

enrichment_repo = EnrichmentRepo()
try:
    _enr_conn = enrichment_db.get_connection()
    _enr_conn.close()
except Exception as _enr_err:
    logging.error("Failed to open enrichment.db: %s", _enr_err)

enrichment_scheduler = RefreshScheduler(
    emit=lambda event, data: socketio.emit(event, data),
)

# Phase 1 real sources.
# TaggerCatalogueSource runs monthly and must be registered before
# TaggerSource so the scheduler list shows it clearly. Its monthly cron
# runs on the 1st of each month at 02:30 UTC — before TaggerSource's
# Sunday 03:00 UTC job.
_PHASE1_SOURCES = [
    (TaggerCatalogueSource(), "0 2 1 * *"),  # monthly: 1st of month 02:30 UTC
    (TaggerSource(),    "weekly"),
    (EDHRECSource(),    "weekly"),
    (EDHTop16Source(),  "weekly"),
    (SpellbookSource(), "weekly"),
]
for _src, _cron in _PHASE1_SOURCES:
    try:
        enrichment_scheduler.register(_src, cron=_cron)
    except Exception as _src_err:
        logging.error("Could not register source %s: %s", _src.name, _src_err)

# Phase 3 real sources
try:
    enrichment_scheduler.register(CardKingdomBuylistSource(), cron="daily")
except Exception as _ck_err:
    logging.error("Could not register buylist_ck source: %s", _ck_err)

# Note: every prior "stub" source now has a real implementation registered
# above. The Scryfall bulk + prices refresh path is wired through the
# /api/database/* endpoints (see db_updater + the Card data card in the
# Settings modal) rather than through the enrichment scheduler, because
# it's an operator-triggered slow rebuild rather than a daily cron job.
# Keeping it out of the scheduler avoids the misleading "prices" entry
# in the Data & Sources panel that used to do nothing.


# =========================================================================
# Pages
# =========================================================================

@app.route('/')
def index():
    from config import APP_NAME
    from support_bundle import _git_head_sha
    # Try to surface the current git short SHA in the About modal so the
    # user (and any support bundle reader) can pin down which build is
    # running. Falls back to "dev" when the worktree isn't a git checkout
    # or when reading HEAD fails for any reason — defensive because this
    # render path must never 500.
    try:
        version = _git_head_sha(SCRIPT_DIR) or 'dev'
    except Exception:
        version = 'dev'
    return render_template(
        'index.html', app_name=APP_NAME, app_version=version,
    )


# =========================================================================
# API documentation (auto-generated OpenAPI + Swagger UI page)
# =========================================================================
#
# /api/openapi.json — live spec generated on demand by introspecting the
# app's url_map. Always reflects the running build, so consumers don't
# need to know whether the docs/openapi.json file on disk is fresh.
#
# /docs — Swagger UI rendered against /api/openapi.json. Pulls
# swagger-ui from a CDN to keep the asset footprint zero.

@app.route('/api/openapi.json')
def api_openapi_json():
    """Live OpenAPI 3.1 spec for the running app. Auto-generated by
    introspecting Flask's url_map; see tools/generate_openapi.py."""
    from tools.generate_openapi import build_spec
    return jsonify(build_spec(app))


@app.route('/docs')
def api_docs_page():
    """Swagger UI page rendered against /api/openapi.json."""
    return (
        '<!doctype html>\n'
        '<html lang="en"><head><meta charset="utf-8">'
        '<title>Cardomancer API docs</title>'
        '<link rel="stylesheet" '
        'href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css">'
        '<style>body{margin:0}</style>'
        '</head><body>'
        '<div id="swagger-ui"></div>'
        '<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"></script>'
        '<script>'
        'window.onload = () => {'
        '  window.ui = SwaggerUIBundle({'
        '    url: "/api/openapi.json",'
        '    dom_id: "#swagger-ui",'
        '    deepLinking: true,'
        '    presets: [SwaggerUIBundle.presets.apis],'
        '    layout: "BaseLayout"'
        '  });'
        '};'
        '</script>'
        '</body></html>'
    )


# =========================================================================
# Hardware API
# =========================================================================

@app.route('/api/connect', methods=['POST'])
def api_connect():
    worker.enqueue('connect')
    return jsonify({'queued': True})


@app.route('/api/disconnect', methods=['POST'])
def api_disconnect():
    worker.enqueue('disconnect')
    return jsonify({'queued': True})


@app.route('/api/home', methods=['POST'])
def api_home():
    axis = request.json.get('axis') if request.json else None
    worker.enqueue('home', axis=axis)
    return jsonify({'queued': True})


@app.route('/api/home/x', methods=['POST'])
def api_home_x():
    worker.enqueue('home', axis='x')
    return jsonify({'queued': True})


@app.route('/api/home/z', methods=['POST'])
def api_home_z():
    worker.enqueue('home', axis='z')
    return jsonify({'queued': True})


@app.route('/api/estop', methods=['POST'])
def api_estop():
    """Emergency stop — bypasses queue, writes directly to serial."""
    worker.emergency_stop()
    return jsonify({'stopped': True})


@app.route('/api/status')
def api_status():
    return jsonify(worker.get_status())


# =========================================================================
# Camera API
# =========================================================================

@app.route('/api/camera/start', methods=['POST'])
def api_camera_start():
    ok = camera.start()
    return jsonify({'started': ok})


@app.route('/api/camera/stop', methods=['POST'])
def api_camera_stop():
    camera.stop()
    return jsonify({'stopped': True})


@app.route('/api/camera/feed')
def api_camera_feed():
    """MJPEG stream for live camera view."""
    if not camera.is_active:
        camera.start()
    return Response(
        camera.generate_mjpeg(quality=70, max_fps=15),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route('/api/camera/feed-aruco')
def api_camera_feed_aruco():
    """
    MJPEG stream with live ArUco marker overlay. Detected markers are
    outlined and labeled with ID + type. Used by the Calibration tab to
    verify that markers are visible to the camera before (and during) a
    sweep.
    """
    if not camera.is_active:
        camera.start()
    return Response(
        camera.generate_mjpeg_with_aruco(quality=70, max_fps=12),
        mimetype='multipart/x-mixed-replace; boundary=frame'
    )


@app.route('/api/camera/snapshot', methods=['POST'])
def api_camera_snapshot():
    """Capture a single frame as JPEG."""
    jpeg = camera.get_jpeg(quality=90)
    if jpeg is None:
        return jsonify({'error': 'No frame available'}), 503
    return Response(jpeg, mimetype='image/jpeg')


@app.route('/api/camera/status')
def api_camera_status():
    return jsonify(camera.get_status())


# =========================================================================
# Bins API
# =========================================================================

@app.route('/api/bins/config', methods=['GET'])
def api_bins_config_get():
    import gcode_control
    bin_locs = gcode_control.get_bin_locations()
    return jsonify({
        'locations': {str(k): v for k, v in bin_locs.items()},
        'bin_width': gcode_control.BIN_WIDTH,
        'source_x': gcode_control.X_SOURCE_BIN,
        'detection_x': gcode_control.X_DETECTION_POSITION,
        'staging_x': gcode_control.X_STAGING_POSITION,
        'staging_width': gcode_control.STAGING_WIDTH,
    })


def _auto_save_bin_config():
    """Save current bin locations + machine positions to _default.json.

    Called automatically whenever bin positions or machine positions change
    through the UI, so the configuration survives server restarts.
    """
    try:
        import gcode_control
        bin_locs = gcode_control.get_bin_locations()
        bin_count = max((k for k in bin_locs if k > 0), default=0)
        spacing = bin_locs.get(2, 200) - bin_locs.get(1, 100) if bin_count >= 2 else 100
        start_x = bin_locs.get(1, 100)

        config = {
            'name': 'Default',
            'bin_count': bin_count,
            'start_x': start_x,
            'spacing': spacing,
            'source_x': bin_locs.get(0, gcode_control.X_SOURCE_BIN),
            'detection_x': gcode_control.X_DETECTION_POSITION,
            'staging_x': gcode_control.X_STAGING_POSITION,
            'staging_width': gcode_control.STAGING_WIDTH,
            'locations': {str(k): v for k, v in bin_locs.items()},
        }

        bin_configs_dir = os.path.join(SCRIPT_DIR, 'bin_configs')
        os.makedirs(bin_configs_dir, exist_ok=True)
        default_path = os.path.join(bin_configs_dir, '_default.json')
        with open(default_path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.warning(f"failed to auto-save bin config: {e}")


@app.route('/api/bins/config', methods=['POST'])
def api_bins_config_set():
    data = request.json or {}
    worker.enqueue('configure_bins',
                   bin_count=data.get('count', 10),
                   start_x=data.get('start_x'),
                   spacing=data.get('spacing'))
    return jsonify({'queued': True})


@app.route('/api/bins/locations', methods=['POST'])
def api_bins_locations_set():
    """Set individual bin X positions manually.
    Expects: {locations: {bin_num: x_pos, ...}}
    Auto-saves to _default.json so positions survive restarts.
    """
    import gcode_control
    data = request.json or {}
    locations = data.get('locations', {})
    if locations:
        gcode_control.set_bin_locations(locations)
        socketio.emit('bins_configured', {'count': len(locations)})
        _auto_save_bin_config()
    return jsonify({'ok': True})


@app.route('/api/bins/machine-positions', methods=['GET'])
def api_bins_machine_positions_get():
    """Get machine reference positions."""
    import gcode_control
    return jsonify({
        'source_x': gcode_control.X_SOURCE_BIN,
        'detection_x': gcode_control.X_DETECTION_POSITION,
        'staging_x': gcode_control.X_STAGING_POSITION,
        'staging_width': gcode_control.STAGING_WIDTH,
    })


@app.route('/api/bins/machine-positions', methods=['POST'])
def api_bins_machine_positions_set():
    """Set machine reference positions (source, detection, staging)."""
    import gcode_control
    data = request.json or {}
    gcode_control.set_machine_positions(
        source_x=data.get('source_x'),
        detection_x=data.get('detection_x'),
        staging_x=data.get('staging_x'),
        staging_width=data.get('staging_width'),
    )
    # Refresh locations in case source_x changed bin 0
    socketio.emit('bins_configured', {})
    _auto_save_bin_config()
    return jsonify({'ok': True})


@app.route('/api/bins/test/<int:bin_number>', methods=['POST'])
def api_bins_test(bin_number):
    worker.enqueue('test_bin', bin_number=bin_number)
    return jsonify({'queued': True})


@app.route('/api/bins/probe/<int:bin_number>', methods=['POST'])
def api_bins_probe(bin_number):
    worker.enqueue('probe_bin', bin_number=bin_number)
    return jsonify({'queued': True})


@app.route('/api/bins/probe-all', methods=['POST'])
def api_bins_probe_all():
    worker.enqueue('probe_all_bins')
    return jsonify({'queued': True})


@app.route('/api/bins/saved-configs')
def api_bins_saved_configs():
    """List saved bin configurations."""
    bin_configs_dir = os.path.join(SCRIPT_DIR, 'bin_configs')
    os.makedirs(bin_configs_dir, exist_ok=True)
    configs = []
    for f in sorted(os.listdir(bin_configs_dir)):
        if f.endswith('.json'):
            filepath = os.path.join(bin_configs_dir, f)
            try:
                with open(filepath, 'r') as fh:
                    data = json.load(fh)
                configs.append({
                    'filename': f,
                    'name': data.get('name', f.replace('.json', '')),
                    'bin_count': data.get('bin_count', '?'),
                    'spacing': data.get('spacing', '?'),
                })
            except Exception:
                configs.append({'filename': f, 'name': f, 'bin_count': '?', 'spacing': '?'})
    return jsonify({'configs': configs})


@app.route('/api/bins/saved-configs/<filename>', methods=['GET'])
def api_bins_saved_config_get(filename):
    """Load a saved bin configuration."""
    bin_configs_dir = os.path.join(SCRIPT_DIR, 'bin_configs')
    filepath = os.path.join(bin_configs_dir, filename)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Config not found'}), 404
    with open(filepath, 'r') as f:
        data = json.load(f)
    return jsonify(data)


@app.route('/api/bins/saved-configs/<filename>', methods=['POST'])
def api_bins_saved_config_save(filename):
    """Save current bin configuration."""
    import gcode_control
    if not filename.endswith('.json'):
        filename += '.json'
    bin_configs_dir = os.path.join(SCRIPT_DIR, 'bin_configs')
    os.makedirs(bin_configs_dir, exist_ok=True)
    filepath = os.path.join(bin_configs_dir, filename)

    bin_locs = gcode_control.get_bin_locations()
    bin_count = max((k for k in bin_locs if k > 0), default=0)
    # Infer spacing from first two sort bins
    spacing = bin_locs.get(2, 200) - bin_locs.get(1, 100) if bin_count >= 2 else 100
    start_x = bin_locs.get(1, 100)

    data = request.json or {}
    config = {
        'name': data.get('name', filename.replace('.json', '')),
        'bin_count': bin_count,
        'start_x': start_x,
        'spacing': spacing,
        'source_x': bin_locs.get(0, gcode_control.X_SOURCE_BIN),
        'detection_x': gcode_control.X_DETECTION_POSITION,
        'staging_x': gcode_control.X_STAGING_POSITION,
        'staging_width': gcode_control.STAGING_WIDTH,
        'locations': {str(k): v for k, v in bin_locs.items()},
    }
    with open(filepath, 'w') as f:
        json.dump(config, f, indent=2)
    return jsonify({'saved': True, 'filename': filename})


@app.route('/api/bins/saved-configs/<filename>', methods=['DELETE'])
def api_bins_saved_config_delete(filename):
    """Delete a saved bin configuration."""
    bin_configs_dir = os.path.join(SCRIPT_DIR, 'bin_configs')
    filepath = os.path.join(bin_configs_dir, filename)
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({'deleted': True})
    return jsonify({'error': 'Config not found'}), 404


@app.route('/api/bins/contents')
def api_bins_contents():
    return jsonify(worker.get_bin_contents())


# -------------------------------------------------------------------------
# Bin Overflow / Fullness API
# -------------------------------------------------------------------------

@app.route('/api/bins/overflow', methods=['GET'])
def api_bins_overflow_get():
    """Get the overflow map and bin fullness status."""
    return jsonify(worker.get_bin_fullness_status())


@app.route('/api/bins/overflow', methods=['POST'])
def api_bins_overflow_set():
    """Set the overflow chain mapping and optionally the card limit."""
    data = request.json or {}
    overflow_map = data.get('overflow_map')
    worker.enqueue('set_overflow_map', overflow_map=overflow_map)
    if 'card_limit' in data:
        worker.enqueue('set_bin_card_limit', limit=int(data['card_limit']))
    return jsonify({'queued': True})


@app.route('/api/bins/card-limit', methods=['POST'])
def api_bins_card_limit():
    """Set the max cards per physical bin."""
    data = request.json or {}
    limit = int(data.get('limit', 150))
    worker.enqueue('set_bin_card_limit', limit=limit)
    return jsonify({'queued': True})


@app.route('/api/bins/mark-empty', methods=['POST'])
def api_bins_mark_empty():
    """Mark a physical bin as emptied (resets card count and full status)."""
    data = request.json or {}
    bin_number = data.get('bin')
    if bin_number is None:
        return jsonify({'error': 'bin number required'}), 400
    worker.enqueue('mark_bin_empty', bin_number=int(bin_number))
    return jsonify({'queued': True})


@app.route('/api/bins/fullness')
def api_bins_fullness():
    """Lightweight endpoint: just bin card counts and full status."""
    return jsonify(worker.get_bin_fullness_status())


# =========================================================================
# Sort Configuration API
# =========================================================================

@app.route('/api/sort/modes')
def api_sort_modes():
    # Legacy endpoint — kept so any older clients that still poll it don't
    # break. Phase 0.1 removed the parallel "mode" concept from the UI;
    # every preset is now a file under sort_configs/ and the client picks
    # one via /api/sort/configs. `custom_manual` was a synthetic mode that
    # meant "post custom_queries dict"; the new UI posts config_lines
    # directly so that mode is no longer advertised.
    return jsonify({
        'modes': {k: v for k, v in SORTING_MODES.items()},
        'descriptions': {
            'color': 'Sort by card color (W/U/B/R/G/Colorless/Multi/Lands)',
            'mana_value': 'Sort by converted mana cost (1-7, 8+)',
            'set': 'Sort by set code',
            'price': 'Sort by USD price tiers',
            'type': 'Sort by card type (Creature/Artifact/Enchantment/etc)',
        }
    })


# ---------------------------------------------------------------------------
# Preset filename hardening.
#
# Phase 0.1 unified every sort preset (built-ins + user saves + one-shot
# custom queries) behind sort_configs/*.txt. A single preset dropdown is
# the only entry point, so the filename becomes the primary key. Keep
# the sanitizer strict:
#
#   - reject absolute paths and any path separator (".." traversal,
#     "configs/other", "C:\...", etc.)
#   - reject hidden / empty names
#   - normalize trailing ".txt"
#
# We also expose the set of built-in presets via a module-level constant
# so the client can render the "(built-in)" badge without hardcoding the
# list in JS (the source of truth is here).
# ---------------------------------------------------------------------------

BUILTIN_SORT_PRESETS = frozenset({
    'color.txt', 'mana_value.txt', 'price.txt', 'price_tiers.txt',
    'set.txt', 'type.txt', 'color_type.txt', 'edh_staples.txt',
})


def _safe_preset_filename(raw):
    """Validate + normalize a preset filename, returning the basename.

    Returns (filename, error_string). filename is None iff error_string is set.
    """
    if not raw or not isinstance(raw, str):
        return None, 'Filename is required'
    name = raw.strip()
    if not name:
        return None, 'Filename is required'
    # Reject anything that looks like a path. Flask's <filename> converter
    # already blocks "/", but users can still type ".." or back-slashes,
    # and we're about to join against SORT_CONFIGS_DIR.
    if '/' in name or '\\' in name or name.startswith('.') or name == '..':
        return None, 'Invalid filename'
    if os.path.basename(name) != name:
        return None, 'Invalid filename'
    if not name.endswith('.txt'):
        name = name + '.txt'
    # Double-check against traversal after appending .txt.
    target = os.path.abspath(os.path.join(SORT_CONFIGS_DIR, name))
    root = os.path.abspath(SORT_CONFIGS_DIR)
    if not target.startswith(root + os.sep) and target != root:
        return None, 'Invalid filename'
    return name, None


def _describe_preset(content):
    """Extract a short description + bin count from preset text.

    The first "# ..." comment line is treated as the preset description
    (matches the convention used by every built-in). `bins:` is parsed
    leniently — a missing/invalid value returns None so the UI can fall
    back gracefully without blocking on a malformed preset.
    """
    description = ''
    bin_count = None
    for raw in (content or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith('#') and not description:
            description = line.lstrip('#').strip()
            continue
        if line.lower().startswith('bins:'):
            try:
                bin_count = int(line.split(':', 1)[1].strip())
            except (ValueError, IndexError):
                bin_count = None
    return description, bin_count


@app.route('/api/sort/configs')
def api_sort_configs():
    """List all sort preset files with metadata.

    Phase 0.1: this is the single source for the preset dropdown. We ship
    description + bin_count + builtin so the client can render a rich
    option list without re-reading every file.
    """
    os.makedirs(SORT_CONFIGS_DIR, exist_ok=True)
    configs = []
    for f in sorted(os.listdir(SORT_CONFIGS_DIR)):
        if not f.endswith('.txt'):
            continue
        filepath = os.path.join(SORT_CONFIGS_DIR, f)
        try:
            with open(filepath, 'r', encoding='utf-8') as fh:
                content = fh.read()
        except OSError:
            content = ''
        description, bin_count = _describe_preset(content)
        configs.append({
            'filename': f,
            'size': os.path.getsize(filepath),
            'description': description,
            'bin_count': bin_count,
            'builtin': f in BUILTIN_SORT_PRESETS,
        })
    return jsonify({
        'configs': configs,
        'builtins': sorted(BUILTIN_SORT_PRESETS),
    })


@app.route('/api/sort/configs/<path:filename>', methods=['GET'])
def api_sort_config_get(filename):
    name, err = _safe_preset_filename(filename)
    if err:
        return jsonify({'error': err}), 400
    filepath = os.path.join(SORT_CONFIGS_DIR, name)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Config not found'}), 404
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    description, bin_count = _describe_preset(content)
    return jsonify({
        'filename': name,
        'content': content,
        'description': description,
        'bin_count': bin_count,
        'builtin': name in BUILTIN_SORT_PRESETS,
    })


@app.route('/api/sort/configs/<path:filename>', methods=['POST'])
def api_sort_config_save(filename):
    name, err = _safe_preset_filename(filename)
    if err:
        return jsonify({'error': err}), 400
    if name in BUILTIN_SORT_PRESETS:
        return jsonify({
            'error': 'Built-in presets are read-only. Use Duplicate/Save As.',
        }), 403
    os.makedirs(SORT_CONFIGS_DIR, exist_ok=True)
    filepath = os.path.join(SORT_CONFIGS_DIR, name)
    payload = request.json or {}
    content = payload.get('content', '')
    if not isinstance(content, str):
        return jsonify({'error': 'content must be a string'}), 400
    # Reject blank files outright — saving an empty preset serves no
    # purpose and later load attempts would fail in SortConfig.from_lines
    # with a confusing error.
    if not content.strip():
        return jsonify({'error': 'Preset content is empty'}), 400
    # Validate parseability unless the caller explicitly opts out via
    # ?skip_validation=1 (reserved for future UI that wants to save a
    # draft mid-edit). Default is strict.
    if not request.args.get('skip_validation'):
        try:
            from sort_config import SortConfig
            SortConfig.from_lines(content.splitlines())
        except Exception as e:
            return jsonify({
                'error': 'invalid_content',
                'message': f'Preset failed to parse: {e}',
            }), 400
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(content)
    return jsonify({'saved': True, 'filename': name})


@app.route('/api/sort/configs/<path:filename>', methods=['DELETE'])
def api_sort_config_delete(filename):
    name, err = _safe_preset_filename(filename)
    if err:
        return jsonify({'error': err}), 400
    if name in BUILTIN_SORT_PRESETS:
        return jsonify({'error': 'Built-in presets cannot be deleted'}), 403
    filepath = os.path.join(SORT_CONFIGS_DIR, name)
    if not os.path.exists(filepath):
        return jsonify({'error': 'Config not found'}), 404
    os.remove(filepath)
    return jsonify({'deleted': True, 'filename': name})


@app.route('/api/sort/configs/<path:filename>/duplicate', methods=['POST'])
def api_sort_config_duplicate(filename):
    """Copy an existing preset to a new filename.

    Body: {"target": "new_name"} — ".txt" is appended if missing. If the
    target already exists, respond 409 so the UI can prompt the user for
    a different name instead of silently clobbering.
    """
    src_name, err = _safe_preset_filename(filename)
    if err:
        return jsonify({'error': f'source: {err}'}), 400
    src_path = os.path.join(SORT_CONFIGS_DIR, src_name)
    if not os.path.exists(src_path):
        return jsonify({'error': 'Source preset not found'}), 404
    payload = request.json or {}
    target_raw = payload.get('target')
    target_name, err = _safe_preset_filename(target_raw)
    if err:
        return jsonify({'error': f'target: {err}'}), 400
    if target_name == src_name:
        return jsonify({'error': 'Target must differ from source'}), 400
    target_path = os.path.join(SORT_CONFIGS_DIR, target_name)
    if os.path.exists(target_path):
        return jsonify({
            'error': 'target_exists',
            'message': f'"{target_name}" already exists',
        }), 409
    with open(src_path, 'r', encoding='utf-8') as f:
        content = f.read()
    with open(target_path, 'w', encoding='utf-8') as f:
        f.write(content)
    return jsonify({
        'duplicated': True,
        'source': src_name,
        'filename': target_name,
    })


# ---------------------------------------------------------------------------
# /api/sort/set-config was the per-mode Set-sort endpoint. It wrote a
# process-global list of set-code→bin mappings consumed by
# sorting.get_bin_for_set(). That entire dispatch path was removed when
# SortConfig took over unified routing; Set-mode now loads
# sort_configs/set.txt and edits happen through /api/sort/configs/*.
# ---------------------------------------------------------------------------


@app.route('/api/sort/validate-query', methods=['POST'])
def api_sort_validate_query():
    query_str = (request.json or {}).get('query', '')
    try:
        from query_parser import parse_query
        ast = parse_query(query_str)
        return jsonify({'valid': True, 'query': query_str})
    except Exception as e:
        return jsonify({'valid': False, 'error': str(e)})


@app.route('/api/sort/current')
def api_sort_current():
    """Return the active SortConfig's per-bin routing, if any.

    Used by the Dashboard "Bin Routing" card (Phase 4.23) to show
    which query is targeting which bin alongside live card counts.
    """
    cfg = getattr(worker, 'sort_config_obj', None)
    if cfg is None:
        return jsonify({'active': False})
    bin_queries = {str(k): v for k, v in (cfg.bin_queries or {}).items()}
    return jsonify({
        'active': True,
        'bin_count': cfg.bin_count,
        'fallback_bin': cfg.fallback_bin,
        'bin_queries': bin_queries,
        'overrides': sorted(getattr(cfg, 'overrides', []) or []),
    })


# =========================================================================
# Sort Session API
# =========================================================================

@app.route('/api/session/start', methods=['POST'])
def api_session_start():
    # Session start requires a connected, idle machine. Previously this
    # endpoint would silently enqueue start_session on a disconnected
    # machine, the worker would flip state to 'sorting', and then the
    # first Detect press would do nothing — causing the "sort does
    # nothing" bug. Fail fast with a clear error instead.
    guard = _ensure_hardware_ready()
    if guard is not None:
        return guard

    # Also require that bins have actually been configured. Starting a
    # session with no known bin positions is a guaranteed no-op.
    import gcode_control
    bin_locs = gcode_control.get_bin_locations() or {}
    dest_bins = [b for b in bin_locs.keys() if b > 0]
    if not dest_bins:
        return jsonify({
            'error': 'no_bins_configured',
            'message': ('No destination bins are configured. Run the '
                        'New Hardware Setup (Calibration tab) or load a '
                        'saved bin config before starting a session.'),
        }), 409
    if not camera.is_active:
        return jsonify({
            'error': 'camera_not_active',
            'message': ('Camera is not active. Start the camera on the '
                        'Dashboard before starting a session.'),
        }), 409

    # Preflight: require a source bin so we have somewhere to pick from.
    source_bins = [b for b in bin_locs.keys() if b <= 0]
    if not source_bins and not getattr(worker, 'source_bins', None):
        return jsonify({
            'error': 'no_source_configured',
            'message': ('No source bin is configured. Run the New '
                        'Hardware Setup so the sorter knows where to '
                        'pick cards from.'),
        }), 409

    # Preflight: staging position must be set. X_CAMERA_POSITION is
    # derived from staging_x — if that's still at its default (no
    # calibration run) sorting will pull from the wrong spot.
    try:
        staging_x = getattr(gcode_control, 'X_STAGING_POSITION', None)
        camera_x = getattr(gcode_control, 'X_CAMERA_POSITION', None)
        if staging_x is None or camera_x is None:
            return jsonify({
                'error': 'no_staging_configured',
                'message': ('Staging position is not configured. Run '
                            'the New Hardware Setup.'),
            }), 409
    except Exception:
        pass

    # Preflight: hash DBs must exist or recognition will fall back
    # through to OCR for every card (which we know is unreliable).
    try:
        import config as _cfg
        missing = []
        if not os.path.exists(_cfg.HASH_DB_PATH):
            missing.append('v1 hash DB')
        if not os.path.exists(_cfg.HASH_DB_V2_PATH):
            missing.append('v2 hash DB')
        if missing:
            return jsonify({
                'error': 'hash_db_missing',
                'message': (f"Missing: {', '.join(missing)}. Run the "
                            f"Database Management update first."),
            }), 409
    except Exception:
        pass

    # Preflight: worker queue not flooded.
    try:
        depth = worker.queue_depth()
        if depth > 10:
            return jsonify({
                'error': 'worker_busy',
                'message': (f'Worker queue depth is {depth} — something '
                            f'is backed up. Wait or restart the server.'),
            }), 409
    except Exception:
        pass

    data = request.json or {}
    mode = data.get('mode', 'color')
    worker.enqueue('start_session',
                   mode=mode,
                   config_file=data.get('config_file'),
                   config_lines=data.get('config_lines'),
                   custom_queries=data.get('custom_queries'),
                   overflow_map=data.get('overflow_map'),
                   notes=data.get('notes'),
                   camera=camera)
    return jsonify({'queued': True})


@app.route('/api/session/confirm-staging-capture', methods=['POST'])
def api_session_confirm_staging_capture():
    """User confirms the staging platform is clear — unblock the worker."""
    worker.confirm_staging_capture()
    return jsonify({'ok': True})


@app.route('/api/session/staging-snapshot', methods=['GET'])
def api_staging_snapshot():
    """Return the snapshot captured for staging ROI drawing."""
    frame = getattr(worker, '_staging_snapshot_frame', None)
    if frame is None:
        return jsonify({'error': 'No staging snapshot available'}), 404
    ret, jpeg = cv2.imencode('.jpg', frame,
                             [cv2.IMWRITE_JPEG_QUALITY, 92])
    if not ret:
        return jsonify({'error': 'Encode failed'}), 500
    return Response(jpeg.tobytes(), mimetype='image/jpeg')


@app.route('/api/session/confirm-focus', methods=['POST'])
def api_session_confirm_focus():
    """User confirms the live feed is in focus — lock autofocus and proceed."""
    worker.confirm_focus_lock()
    return jsonify({'ok': True})


@app.route('/api/session/set-staging-roi', methods=['POST'])
def api_set_staging_roi():
    """
    User submits 4 corner points for the staging platform ROI.
    Expects JSON: { "corners": [[x1,y1], [x2,y2], [x3,y3], [x4,y4]] }
    Coordinates are in pixel space of the staging snapshot.
    """
    data = request.json or {}
    corners = data.get('corners')
    if not corners or len(corners) != 4:
        return jsonify({'error': 'Need exactly 4 corner points'}), 400
    worker.set_staging_roi(corners)
    return jsonify({'ok': True})


@app.route('/api/session/detect', methods=['POST'])
def api_session_detect():
    # Detect must run during a 'sorting' session, so we can't use the
    # generic _ensure_hardware_ready() guard (which rejects the sorting
    # state). Do the connection check inline.
    import gcode_control
    if not gcode_control.is_connected():
        return jsonify({
            'error': 'not_connected',
            'message': 'Machine is not connected.',
        }), 409
    if worker.state != 'sorting':
        return jsonify({
            'error': 'not_sorting',
            'message': (f'Cannot detect — session state is '
                        f'"{worker.state}", must be "sorting". '
                        f'Press Start Session first.'),
        }), 409
    worker.enqueue('detect_and_sort', camera=camera)
    return jsonify({'queued': True})


@app.route('/api/session/pause', methods=['POST'])
def api_session_pause():
    worker.enqueue('pause')
    return jsonify({'queued': True})


@app.route('/api/session/resume', methods=['POST'])
def api_session_resume():
    worker.enqueue('resume')
    return jsonify({'queued': True})


@app.route('/api/session/stop', methods=['POST'])
def api_session_stop():
    worker.enqueue('stop_session')
    return jsonify({'queued': True})


@app.route('/api/session/status')
def api_session_status():
    status = worker.get_status()
    status['bin_contents'] = worker.get_bin_contents()
    return jsonify(status)


@app.route('/api/session/continuous/start', methods=['POST'])
def api_continuous_start():
    data = request.json or {}
    delay = data.get('delay', 0.5)
    worker.enqueue('start_continuous', camera=camera, delay=float(delay))
    return jsonify({'queued': True})


@app.route('/api/session/continuous/stop', methods=['POST'])
def api_continuous_stop():
    worker.enqueue('stop_continuous')
    return jsonify({'queued': True})


@app.route('/api/session/undo', methods=['POST'])
def api_session_undo():
    worker.enqueue('undo_last_sort', camera=camera)
    return jsonify({'queued': True})


@app.route('/api/test-scan/start', methods=['POST'])
def api_test_scan_start():
    data = request.json or {}
    count = int(data.get('count', 10))
    drop_bin = int(data.get('drop_bin', 1))
    worker.enqueue('test_scan', camera=camera, count=count, drop_bin=drop_bin)
    return jsonify({'queued': True})


@app.route('/api/test-scan/stop', methods=['POST'])
def api_test_scan_stop():
    worker.test_scan_stop = True
    return jsonify({'ok': True})


@app.route('/api/session/wishlist-bin', methods=['POST'])
def api_session_wishlist_bin():
    data = request.json or {}
    bin_number = data.get('bin')  # None to disable
    if bin_number is not None:
        bin_number = int(bin_number)
    worker.enqueue('set_wishlist_bin', bin_number=bin_number)
    return jsonify({'queued': True})


# --- Priority bin (Phase 4.21) ---

@app.route('/api/session/priority-bin', methods=['POST'])
def api_session_priority_bin():
    """
    Configure the priority-bin route.  Body:
        {"bin": <int|null>, "wishlist_source": "moxfield:<user>"|null}
    Both null => disabled.
    """
    data = request.json or {}
    bin_number = data.get('bin')
    wishlist_source = data.get('wishlist_source')
    if bin_number is not None:
        bin_number = int(bin_number)
    worker.enqueue('set_priority_bin',
                   bin_number=bin_number,
                   wishlist_source=wishlist_source)
    return jsonify({'queued': True})


@app.route('/api/integrations/moxfield/wishlist/list')
def api_integrations_moxfield_wishlist_list():
    """List cached Moxfield wishlist sources (for UI dropdown)."""
    import collection_db
    conn = collection_db.get_connection()
    try:
        items = collection_db.list_moxfield_wishlists(conn)
    finally:
        conn.close()
    return jsonify({'wishlists': items})


@app.route('/api/integrations/moxfield/wishlist/upsert', methods=['POST'])
def api_integrations_moxfield_wishlist_upsert():
    """
    Create or replace a cached Moxfield wishlist.  Body:
        {"source_key": "moxfield:<user>",
         "username": "<user>",
         "display_name": "...",
         "cards": [{"oracle_id": "...", "name": "...",
                    "set_code": "...", "image_uri": "..."}]}

    Used by tests and by the (future) Moxfield sync worker.  Basic
    lands are filtered out at upsert time.
    """
    import collection_db
    data = request.json or {}
    source_key = data.get('source_key')
    if not source_key:
        return jsonify({'error': 'source_key required'}), 400
    conn = collection_db.get_connection()
    try:
        wid = collection_db.upsert_moxfield_wishlist(
            conn,
            source_key=source_key,
            cards=data.get('cards') or [],
            username=data.get('username'),
            display_name=data.get('display_name'),
        )
    finally:
        conn.close()
    return jsonify({'id': wid, 'source_key': source_key})


@app.route('/api/integrations/moxfield/wishlist/paste', methods=['POST'])
def api_integrations_moxfield_wishlist_paste():
    """Ingest a Moxfield plain-text decklist and cache it as a wishlist source.

    Replaces the auth'd API pull: user pastes text from Moxfield's
    "Export deck" (or wishlist) and we resolve names → oracle_ids against
    the local Scryfall bulk cache.

    Body:
        {"source_key": "moxfield:<label>",   # required, e.g. "moxfield:my-edh"
         "display_name": "...",              # optional
         "text": "4 Lightning Bolt\\n..."}   # required

    Returns:
        {"id": int, "source_key": str, "card_count": int,
         "unresolved": [name, ...]}
    """
    import collection_db
    from web_enrichment import moxfield_text

    data = request.json or {}
    source_key = (data.get('source_key') or '').strip()
    text = data.get('text') or ''
    if not source_key:
        return jsonify({'error': 'source_key required'}), 400
    if not text.strip():
        return jsonify({'error': 'text is empty'}), 400

    entries = moxfield_text.parse_text(text)
    resolved, unresolved = moxfield_text.resolve_entries(entries)

    conn = collection_db.get_connection()
    try:
        wid = collection_db.upsert_moxfield_wishlist(
            conn,
            source_key=source_key,
            cards=resolved,
            display_name=data.get('display_name'),
        )
    finally:
        conn.close()
    return jsonify({
        'id': wid,
        'source_key': source_key,
        'card_count': len(resolved),
        'unresolved': unresolved,
    })


@app.route('/api/translate/scryfall')
def api_translate_scryfall():
    """Translate our internal sort-config DSL into a Scryfall search query.

    Drives the bin-query "view on Scryfall" link in the sort-config
    builder and the Query Helper's "view on Scryfall" cross-link.

    Query params:
        q:  the DSL string (required)

    Returns:
        {"q":       "<scryfall query string>",
         "url":     "https://scryfall.com/search?q=...",   // empty if q is empty
         "dropped": ["<predicate>  — <reason>", ...]      // partial translations
        }
    """
    from web_enrichment.scryfall_query_translator import to_scryfall_url
    dsl = (request.args.get('q') or '').strip()
    if not dsl:
        return jsonify({'q': '', 'url': '', 'dropped': []})
    url, dropped = to_scryfall_url(dsl)
    # Reconstruct the bare query from the URL for callers that want it
    # without the URL wrapping.
    from urllib.parse import urlparse, parse_qs
    bare_q = ''
    if url:
        parsed = urlparse(url)
        bare_q = parse_qs(parsed.query).get('q', [''])[0]
    return jsonify({'q': bare_q, 'url': url, 'dropped': dropped})


@app.route('/api/integrations/moxfield/export-text')
def api_integrations_moxfield_export_text():
    """Render current inventory as Moxfield plain-text for copy/paste into Moxfield.

    Query params:
        box:       optional — filter inventory to a single box
        min_qty:   optional int, default 1

    Returns text/plain body.
    """
    import collection_db
    from web_enrichment import moxfield_text

    box = request.args.get('box') or None
    try:
        min_qty = max(1, int(request.args.get('min_qty') or 1))
    except ValueError:
        min_qty = 1

    conn = collection_db.get_connection()
    try:
        rows = collection_db.get_inventory(conn, order_by='name')
    finally:
        conn.close()

    if box:
        rows = [r for r in rows if (r.get('box') or '') == box]
    rows = [r for r in rows
            if (r.get('quantity') or 0) >= min_qty
            or (r.get('foil_quantity') or 0) >= min_qty]

    text = moxfield_text.render_text(rows)
    return Response(text, mimetype='text/plain')


@app.route('/api/session/rehome-interval', methods=['POST'])
def api_session_rehome_interval():
    data = request.json or {}
    interval = int(data.get('interval', 100))
    worker.rehome_interval = max(10, interval)
    return jsonify({'interval': worker.rehome_interval})


@app.route('/api/session/scan-images/<int:scan_num>')
def api_session_scan_image(scan_num):
    """Serve a scan image for the current session."""
    if worker._scan_images_dir and os.path.isdir(worker._scan_images_dir):
        filename = f"scan_{scan_num:04d}.jpg"
        filepath = os.path.join(worker._scan_images_dir, filename)
        if os.path.exists(filepath):
            return send_file(filepath, mimetype='image/jpeg')
    return jsonify({'error': 'Image not found'}), 404


# =========================================================================
# Motion API
# =========================================================================

@app.route('/api/motion/move-x', methods=['POST'])
def api_motion_move_x():
    x = (request.json or {}).get('x', 0)
    worker.enqueue('move_x', x=float(x))
    return jsonify({'queued': True})


@app.route('/api/motion/move-z', methods=['POST'])
def api_motion_move_z():
    z = (request.json or {}).get('z', 220)
    worker.enqueue('move_z', z=float(z))
    return jsonify({'queued': True})


@app.route('/api/motion/position')
def api_motion_position():
    return jsonify(motion_tracker.get_state())


@app.route('/api/motion/detection-position', methods=['POST'])
def api_motion_detection():
    worker.enqueue('move_to_detection')
    return jsonify({'queued': True})


# =========================================================================
# Simulation API
# =========================================================================

@app.route('/api/sim/test-run', methods=['POST'])
def api_sim_test_run():
    data = request.json or {}
    card_count = data.get('card_count', 10)
    mode = data.get('mode', 'color')

    # Phase 0.1: the unified preset UI posts `config_lines` — the raw text
    # content of a preset file. The legacy `custom_file` (load from named
    # preset) and `custom_manual` (inline dict) paths are kept only for
    # back-compat with older test scripts; all new callers should pass
    # config_lines directly.
    sort_config = None
    if data.get('config_lines') is not None:
        from sort_config import SortConfig
        raw = data['config_lines']
        lines = raw.splitlines() if isinstance(raw, str) else list(raw)
        try:
            sort_config = SortConfig.from_lines(lines)
        except Exception as e:
            return jsonify({'error': str(e)}), 400
    elif mode == 'custom_file' and data.get('config_file'):
        from sort_config import SortConfig
        filepath = data['config_file']
        if not os.path.isabs(filepath):
            filepath = os.path.join(SORT_CONFIGS_DIR, filepath)
        try:
            sort_config = SortConfig.from_file(filepath)
        except Exception as e:
            return jsonify({'error': str(e)}), 400
    elif mode == 'custom_manual' and data.get('custom_queries'):
        # Legacy dict form — kept for back-compat. New UI posts config_lines.
        from sort_config import SortConfig
        queries = data['custom_queries']
        lines = ['bins: ' + str(queries.get('bin_count', 10)),
                 'fallback: ' + str(queries.get('fallback_bin', 10))]
        if queries.get('bin_limit'):
            lines.append('limit: ' + str(queries['bin_limit']))
        for bin_num, query_str in queries.get('queries', {}).items():
            if query_str.strip():
                lines.append(f'bin{bin_num}: {query_str}')
        try:
            sort_config = SortConfig.from_lines(lines)
        except Exception as e:
            return jsonify({'error': str(e)}), 400

    def emit_fn(event, data):
        socketio.emit(event, data)
    ok = simulator.start(card_count, mode, sort_config=sort_config, emit_fn=emit_fn)
    return jsonify({'started': ok})


@app.route('/api/sim/stop', methods=['POST'])
def api_sim_stop():
    simulator.stop()
    return jsonify({'stopped': True})


@app.route('/api/sim/status')
def api_sim_status():
    return jsonify(simulator.get_status())


# =========================================================================
# =========================================================================
# Card Lookup API (autocomplete / validation from Scryfall data)
# =========================================================================

# Build name→printings index on first use (lazy init)
_card_name_index = None

def _get_card_name_index():
    global _card_name_index
    if _card_name_index is not None:
        return _card_name_index

    from cards import CARDS_DATA
    index = {}  # lowercase name → { name, printings: [{set, num, colors, type, rarity, price}] }
    for c in CARDS_DATA:
        if c.get('lang') != 'en' or 'paper' not in c.get('games', []):
            continue
        name = c.get('name', '')
        if not name:
            continue
        key = name.lower()
        if key not in index:
            index[key] = {'name': name, 'printings': []}
        prices = c.get('prices', {})
        try:
            price = float(prices.get('usd') or prices.get('usd_foil') or 0)
        except (ValueError, TypeError):
            price = None
        index[key]['printings'].append({
            'set': c.get('set', ''),
            'collector_number': c.get('collector_number', ''),
            'colors': ''.join(c.get('colors', [])),
            'type_line': c.get('type_line', ''),
            'rarity': c.get('rarity', ''),
            'price_usd': price,
            'cmc': c.get('cmc'),
        })
    _card_name_index = index
    logger.info(f"Card name index built: {len(index)} unique names")
    return index


@app.route('/api/cards/autocomplete')
def api_cards_autocomplete():
    q = (request.args.get('q') or '').strip().lower()
    if len(q) < 2:
        return jsonify({'suggestions': []})
    index = _get_card_name_index()
    matches = []
    for key, val in index.items():
        if key.startswith(q) or q in key:
            matches.append(val['name'])
    # Sort: exact match first, then starts-with, then contains, all alphabetical
    matches.sort(key=lambda n: (
        0 if n.lower() == q else 1 if n.lower().startswith(q) else 2,
        n.lower()
    ))
    return jsonify({'suggestions': matches[:20]})


def _build_card_overlay_info(name, set_code=None, card_id=None):
    """Build the payload consumed by the Sort Session live card-info overlay
    (Phase 4 item 4.19).

    Lookup priority:
      1. `card_id` — Scryfall UUID, unambiguous. Always prefer this when
         the caller has it (the identifier in web_worker does) so the
         overlay shows the exact printing that was matched, not some
         other printing of a same-named card (e.g. one of several
         collector-numbered basic Swamps in the same set).
      2. `name` (+ optional `set_code`) — name-based lookup for legacy
         callers that don't pass an ID. Picks the first paper English
         printing whose set matches, or the first printing of any set if
         no match.

    Returns a dict with the fields the overlay renders:
      - name, set, collector_number, oracle_id, scryfall_id
      - image_url  (Scryfall `image_uris.normal`, or None if unavailable)
      - border     (Scryfall `border_color`: "black" / "white" / "silver" /
                    "borderless" / "gold", or None)
      - price_usd  (float or None)

    Returns None if no matching card is found. Pure function — no Flask
    dependency — so it can be unit-tested directly.
    """
    from cards import CARDS_DATA, CARD_DATA_BY_ID

    card = None
    # Preferred path: look up by unique Scryfall ID.
    if card_id:
        card = CARD_DATA_BY_ID.get(card_id)

    if card is None:
        if not name:
            return None
        key = (name or '').strip().lower()
        if not key:
            return None
        set_key = (set_code or '').strip().lower() or None

        best = None  # preferred printing once we find a set match
        fallback = None  # first paper English printing of any set
        for c in CARDS_DATA:
            if c.get('lang') != 'en' or 'paper' not in c.get('games', []):
                continue
            if (c.get('name') or '').lower() != key:
                continue
            if set_key and (c.get('set') or '').lower() == set_key:
                best = c
                break
            if fallback is None:
                fallback = c

        card = best or fallback

    if card is None:
        return None

    # Prefer `normal`; fall back through the Scryfall image_uris priority.
    image_uris = card.get('image_uris') or {}
    image_url = (
        image_uris.get('normal')
        or image_uris.get('large')
        or image_uris.get('small')
        or image_uris.get('png')
    )
    # Some card objects (MDFC, transform) have image_uris nested under
    # card_faces instead of the top level.
    if not image_url:
        faces = card.get('card_faces') or []
        if faces:
            face_uris = (faces[0] or {}).get('image_uris') or {}
            image_url = (
                face_uris.get('normal')
                or face_uris.get('large')
                or face_uris.get('small')
                or face_uris.get('png')
            )

    prices = card.get('prices') or {}
    price_raw = prices.get('usd') or prices.get('usd_foil')
    try:
        price_usd = float(price_raw) if price_raw is not None else None
    except (TypeError, ValueError):
        price_usd = None

    return {
        'name': card.get('name'),
        'set': card.get('set'),
        'collector_number': card.get('collector_number'),
        'oracle_id': card.get('oracle_id'),
        'scryfall_id': card.get('id'),
        'image_url': image_url,
        'border': card.get('border_color'),
        'price_usd': price_usd,
    }


@app.route('/api/card/overlay-info')
def api_card_overlay_info():
    """Return the minimal card-info payload for the Sort Session live
    overlay (Phase 4 item 4.19).

    Query params (at least one of `id` or `name` is required):
      - id   (preferred): Scryfall UUID — picks the exact printing
      - name (legacy):    card name (case-insensitive)
      - set  (optional):  Scryfall set code; used only with `name` to
                          prefer a specific printing

    Response body: see `_build_card_overlay_info`. Returns 400 if neither
    `id` nor `name` is supplied, 404 if no matching card exists in the
    local Scryfall bulk cache.
    """
    card_id = (request.args.get('id') or '').strip() or None
    name = (request.args.get('name') or '').strip()
    set_code = (request.args.get('set') or '').strip() or None
    if not card_id and not name:
        return jsonify({'error': 'id or name required'}), 400
    info = _build_card_overlay_info(name, set_code, card_id=card_id)
    if info is None:
        return jsonify({'error': 'not found',
                        'id': card_id,
                        'name': name}), 404
    return jsonify(info)


@app.route('/api/cards/printings')
def api_cards_printings():
    name = (request.args.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400
    index = _get_card_name_index()
    entry = index.get(name.lower())
    if not entry:
        return jsonify({'name': name, 'printings': []})
    # Sort printings by set code alphabetically, then collector number
    printings = sorted(
        entry['printings'],
        key=lambda p: (p.get('set', '').lower(),
                       p.get('collector_number', '').zfill(5))
    )
    return jsonify({'name': entry['name'], 'printings': printings})


# =========================================================================
# Collection API
# =========================================================================

@app.route('/api/collection/stats')
def api_collection_stats():
    import collection_db
    conn = collection_db.get_connection()
    try:
        stats = collection_db.get_collection_stats(conn)
        return jsonify(stats)
    finally:
        conn.close()


@app.route('/api/collection/inventory')
def api_collection_inventory():
    import collection_db
    conn = collection_db.get_connection()
    try:
        # Pagination
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 50, type=int)
        sort_by = request.args.get('sort', 'name')
        sort_dir = request.args.get('dir', 'ASC')

        # Search filters
        name = request.args.get('name')
        set_code = request.args.get('set')
        colors = request.args.get('colors')
        rarity = request.args.get('rarity')
        type_line = request.args.get('type')
        min_price = request.args.get('min_price', type=float)
        max_price = request.args.get('max_price', type=float)
        box = request.args.get('box')

        if any([name, set_code, colors, rarity, type_line,
                min_price is not None, max_price is not None,
                box is not None]):
            results = collection_db.search_collection(
                conn, name=name, set_code=set_code, colors=colors,
                rarity=rarity, type_line=type_line,
                min_price=min_price, max_price=max_price, box=box,
                order_by=sort_by, order_dir=sort_dir)
        else:
            results = collection_db.get_inventory(
                conn, order_by=sort_by, order_dir=sort_dir)

        total = len(results)
        start = (page - 1) * per_page
        end = start + per_page
        page_results = results[start:end]

        return jsonify({
            'items': page_results,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page if per_page else 0,
        })
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# /api/collection/filter  —  Phase 2 item 2.13 (tag filters)
#
# Runs a compiled query string from the Collection tab filter-chips UI
# through query_parser.evaluate_query against the inventory table,
# enriching each row with staple/salt/combo/buylist data from
# enrichment.db. Accepts the same tokens as sort-config queries
# (staple:*, salt>N, combo:true, buylist:ck, cull:true, usd<X,
# t:creature, c:ur, etc.). On QueryParseError returns 400.
#
# The Collection-tab UI (templates/index.html + static/app.js) compiles
# chip state into a query string and hits this endpoint.
# ---------------------------------------------------------------------------

def _inventory_row_to_card_data(row):
    """Build a Scryfall-card-like dict from an inventory row.

    Uses only the inventory-stored fields so filters like usd<X and
    t:creature reflect the user's collection state at scan time, not
    live Scryfall prices/types. oracle_text and color_identity are left
    minimal here — _enrich_card_data_for_query backfills them from
    card_lookup so o:/ci: queries work without changing price/type
    semantics.
    """
    colors_raw = row.get('colors') or ''
    if ',' in colors_raw:
        colors = [c.strip() for c in colors_raw.split(',') if c.strip()]
    else:
        colors = [c for c in colors_raw if c.strip()]
    price_usd = row.get('price_usd')
    prices = {'usd': str(price_usd) if price_usd is not None else None}
    return {
        'name': row.get('name') or '',
        'oracle_id': row.get('oracle_id'),
        'set': (row.get('set_code') or '').lower(),
        'colors': colors,
        'color_identity': colors,
        'type_line': row.get('type_line') or '',
        'cmc': row.get('cmc'),
        'rarity': (row.get('rarity') or '').lower(),
        'prices': prices,
        'oracle_text': '',
    }


def _enrich_card_data_for_query(card_data, row):
    """Pull oracle_text + color_identity from card_lookup so o:/ci:
    query tokens match correctly. No-op if card_lookup isn't loaded or
    the (set, collector_number) pair isn't in its index — the caller
    keeps the inventory-derived defaults in that case."""
    try:
        import card_lookup
    except Exception:
        return
    set_code = row.get('set_code') or ''
    collector_number = row.get('collector_number') or ''
    if not set_code or not collector_number:
        return
    try:
        full = card_lookup.lookup_by_set_collector(set_code, collector_number)
    except Exception:
        return
    if not full:
        return
    oracle_text = full.get('oracle_text')
    if oracle_text:
        card_data['oracle_text'] = oracle_text
    color_identity = full.get('color_identity')
    if color_identity:
        card_data['color_identity'] = list(color_identity)


def _bulk_fetch_enrichment(enr_conn, oracle_ids):
    """Bulk version of _fetch_enrichment_for_oracle. Replaces N*4 SELECTs
    (one set per oracle_id) with 4 SELECTs per 500-id batch, using IN
    clauses. Returns {oracle_id: enrichment_dict}.

    On any batch failure, logs and continues with the next batch — same
    swallow-and-continue contract as the per-oracle version.
    """
    if not oracle_ids:
        return {}
    result = {}
    chunk_size = 500
    for i in range(0, len(oracle_ids), chunk_size):
        chunk = oracle_ids[i:i + chunk_size]
        placeholders = ','.join(['?'] * len(chunk))
        try:
            staple_rows = enr_conn.execute(
                f"SELECT oracle_id, tier FROM staples WHERE oracle_id IN ({placeholders})",
                chunk,
            ).fetchall()
            salt_rows = enr_conn.execute(
                f"SELECT oracle_id, salt FROM salt_scores WHERE oracle_id IN ({placeholders})",
                chunk,
            ).fetchall()
            combo_rows = enr_conn.execute(
                f"SELECT oracle_id FROM combo_membership WHERE oracle_id IN ({placeholders})",
                chunk,
            ).fetchall()
            buylist_rows = enr_conn.execute(
                f"SELECT oracle_id, price_usd FROM buylists "
                f"WHERE vendor='ck' AND oracle_id IN ({placeholders})",
                chunk,
            ).fetchall()
        except Exception:
            logger.warning("_bulk_fetch_enrichment batch failed",
                           exc_info=True)
            continue

        for oid in chunk:
            result[oid] = {
                'staple_universal': False,
                'staple_cedh': False,
                'staple_archetype': False,
                'salt': None,
                'in_combo': False,
                'buylist_ck_price': None,
            }
        for row in staple_rows:
            tier = row['tier']
            if tier == 'universal':
                result[row['oracle_id']]['staple_universal'] = True
            elif tier == 'cedh':
                result[row['oracle_id']]['staple_cedh'] = True
            elif tier == 'archetype':
                result[row['oracle_id']]['staple_archetype'] = True
        for row in salt_rows:
            result[row['oracle_id']]['salt'] = row['salt']
        for row in combo_rows:
            result[row['oracle_id']]['in_combo'] = True
        for row in buylist_rows:
            result[row['oracle_id']]['buylist_ck_price'] = row['price_usd']
    return result


@app.route('/api/collection/filter')
def api_collection_filter():
    """
    Filter the inventory table using a Scryfall-like query string.

    Query params:
      q         — compiled query (e.g. "staple:universal usd<1.00").
                  Empty or missing means no filter (full inventory).
      page      — 1-indexed page number
      per_page  — page size (default 50)
      sort/dir  — forwarded to collection_db.get_inventory

    Returns: {items, total, page, per_page, pages, query, error?}
    """
    import collection_db
    import query_parser

    q = (request.args.get('q') or '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    sort_by = request.args.get('sort', 'name')
    sort_dir = request.args.get('dir', 'ASC')

    ast = None
    if q:
        try:
            ast = query_parser.parse_query(q)
        except query_parser.QueryParseError as exc:
            return jsonify({
                'items': [], 'total': 0, 'page': page,
                'per_page': per_page, 'pages': 0, 'query': q,
                'error': f'Query syntax error: {exc}',
            }), 400

    conn = collection_db.get_connection()
    enr_conn = None
    try:
        rows = collection_db.get_inventory(
            conn, order_by=sort_by, order_dir=sort_dir
        )

        if ast is None:
            filtered = rows
        else:
            try:
                enr_conn = enrichment_db.get_connection()
            except Exception as exc:
                logger.warning(f"collection/filter: enrichment.db unavailable: {exc}")
                enr_conn = None

            # Bulk-fetch enrichment for every oracle_id in the result set
            # in 4 batched IN-clause SELECTs instead of 4 SELECTs per row
            # (the prior shape was N+1 across distinct oracle_ids — fine
            # for tiny collections, slow once an inventory has thousands).
            if enr_conn is not None:
                unique_oracles = list({
                    row.get('oracle_id') for row in rows if row.get('oracle_id')
                })
                enr_cache = _bulk_fetch_enrichment(enr_conn, unique_oracles)
            else:
                enr_cache = {}

            filtered = []
            parse_error = None
            for row in rows:
                # Build the card-data dict from the inventory row (keeps
                # the user's stored price/type — filtering against live
                # Scryfall would silently change semantics for usd<X /
                # t:creature). Then enrich with oracle_text and a real
                # color_identity from card_lookup when available so o:
                # and ci: query tokens behave correctly (without the
                # enrichment, oracle_text='' meant o: matched nothing
                # and color_identity=colors was wrong for hybrid/devoid).
                card_data = _inventory_row_to_card_data(row)
                _enrich_card_data_for_query(card_data, row)
                oracle_id = row.get('oracle_id')
                enrichment_data = enr_cache.get(oracle_id, {}) if oracle_id else {}

                try:
                    try:
                        matched = query_parser.evaluate_query(
                            ast, card_data,
                            enrichment_data=enrichment_data,
                        )
                    except TypeError:
                        matched = query_parser.evaluate_query(
                            ast, card_data
                        )
                except query_parser.QueryParseError as exc:
                    parse_error = str(exc)
                    break
                except Exception:
                    logger.warning("collection/filter: eval error", exc_info=True)
                    continue

                if matched:
                    filtered.append(row)

            if parse_error is not None:
                return jsonify({
                    'items': [], 'total': 0, 'page': page,
                    'per_page': per_page, 'pages': 0, 'query': q,
                    'error': f'Query syntax error: {parse_error}',
                }), 400

        total = len(filtered)
        start = (page - 1) * per_page
        end = start + per_page
        page_rows = filtered[start:end]
        return jsonify({
            'items': page_rows,
            'total': total,
            'page': page,
            'per_page': per_page,
            'pages': (total + per_page - 1) // per_page if per_page else 0,
            'query': q,
        })
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if enr_conn is not None:
            try:
                enr_conn.close()
            except Exception:
                pass


@app.route('/api/collection/sessions')
def api_collection_sessions():
    """List sessions, most-recent-first.

    Optional `?limit=N` caps the response. Used by the Sort tab's
    post-sort "View past sessions" modal, which typically pulls 25.
    """
    import collection_db
    limit_arg = request.args.get('limit')
    limit = int(limit_arg) if limit_arg else None
    conn = collection_db.get_connection()
    try:
        sessions = collection_db.get_session_history(conn, limit=limit)
        return jsonify({'sessions': sessions})
    finally:
        conn.close()


@app.route('/api/collection/sessions/<int:session_id>')
def api_collection_session_detail(session_id):
    import collection_db
    conn = collection_db.get_connection()
    try:
        scans = collection_db.get_scan_history(conn, session_id=session_id)
        return jsonify({'session_id': session_id, 'scans': scans})
    finally:
        conn.close()


@app.route('/api/collection/export/csv')
def api_collection_export_csv():
    import collection_db
    import csv
    conn = collection_db.get_connection()
    try:
        rows = collection_db.get_inventory(conn, order_by='name')
        if not rows:
            return jsonify({'error': 'No cards in inventory'}), 404

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=collection.csv'}
        )
    finally:
        conn.close()


@app.route('/api/collection/export/decklist')
def api_collection_export_decklist():
    import collection_db
    conn = collection_db.get_connection()
    try:
        box = request.args.get('box')
        if box:
            rows = collection_db.search_collection(conn, box=box,
                                                   order_by='name')
        else:
            rows = collection_db.get_inventory(conn, order_by='name')
        if not rows:
            return jsonify({'error': 'No cards'}), 404
        lines = []
        for r in rows:
            lines.append(f"{r['quantity']}x {r['name']}")
        return Response(
            '\n'.join(lines),
            mimetype='text/plain',
            headers={'Content-Disposition':
                     'attachment; filename=decklist.txt'}
        )
    finally:
        conn.close()


@app.route('/api/collection/inventory/<int:item_id>', methods=['DELETE'])
def api_collection_delete_item(item_id):
    import collection_db
    qty = request.args.get('qty', type=int)
    conn = collection_db.get_connection()
    try:
        collection_db.delete_inventory_item(conn, item_id, quantity=qty)
        return jsonify({'deleted': True})
    finally:
        conn.close()


@app.route('/api/collection/inventory/<int:item_id>/increment', methods=['POST'])
def api_collection_increment_item(item_id):
    import collection_db
    qty = 1
    if request.is_json and request.json.get('quantity'):
        qty = int(request.json['quantity'])
    conn = collection_db.get_connection()
    try:
        collection_db.increment_inventory_item(conn, item_id, quantity=qty)
        return jsonify({'incremented': True})
    finally:
        conn.close()


@app.route('/api/collection/inventory', methods=['POST'])
def api_collection_add_item():
    import collection_db
    data = request.json or {}
    name = (data.get('name') or '').strip()
    set_code = (data.get('set_code') or '').strip()
    if not name or not set_code:
        return jsonify({'error': 'Name and set code are required'}), 400
    conn = collection_db.get_connection()
    try:
        item_id = collection_db.add_inventory_item(
            conn, name=name, set_code=set_code,
            collector_number=(data.get('collector_number') or '').strip(),
            colors=(data.get('colors') or '').strip() or None,
            type_line=(data.get('type_line') or '').strip() or None,
            rarity=(data.get('rarity') or '').strip() or None,
            price_usd=data.get('price_usd'),
            quantity=int(data.get('quantity', 1)),
            box=(data.get('box') or '').strip() or None,
        )
        return jsonify({'id': item_id, 'added': True})
    finally:
        conn.close()


@app.route('/api/collection/sessions/<int:session_id>', methods=['DELETE'])
def api_collection_delete_session(session_id):
    import collection_db
    conn = collection_db.get_connection()
    try:
        collection_db.delete_session(conn, session_id)
        return jsonify({'deleted': True})
    finally:
        conn.close()


@app.route('/api/collection/reset', methods=['POST'])
def api_collection_reset():
    import collection_db
    conn = collection_db.get_connection()
    try:
        collection_db.reset_collection(conn)
        return jsonify({'reset': True})
    finally:
        conn.close()


@app.route('/api/collection/wishlist')
def api_collection_wishlist():
    import collection_db
    conn = collection_db.get_connection()
    try:
        items = collection_db.get_wishlist(conn)
        return jsonify({'items': items})
    finally:
        conn.close()


@app.route('/api/collection/wishlist', methods=['POST'])
def api_collection_wishlist_add():
    import collection_db
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Card name is required'}), 400
    conn = collection_db.get_connection()
    try:
        collection_db.add_wishlist_item(
            conn, name=name,
            set_code=(data.get('set_code') or '').strip() or None,
            max_price=data.get('max_price'),
            priority=data.get('priority', 'normal'),
            notes=(data.get('notes') or '').strip() or None,
        )
        return jsonify({'added': True})
    finally:
        conn.close()


@app.route('/api/collection/wishlist/<int:item_id>', methods=['DELETE'])
def api_collection_wishlist_delete(item_id):
    import collection_db
    conn = collection_db.get_connection()
    try:
        collection_db.delete_wishlist_item(conn, item_id)
        return jsonify({'deleted': True})
    finally:
        conn.close()


@app.route('/api/collection/wishlist/<int:item_id>/found', methods=['POST'])
def api_collection_wishlist_found(item_id):
    import collection_db
    conn = collection_db.get_connection()
    try:
        collection_db.mark_wishlist_found(conn, item_id)
        return jsonify({'marked': True})
    finally:
        conn.close()


@app.route('/api/collection/boxes/labels')
def api_collection_box_labels():
    import collection_db
    conn = collection_db.get_connection()
    try:
        summary = collection_db.get_box_summary(conn)
        from datetime import datetime
        date_str = datetime.now().strftime('%Y-%m-%d')
        html = f"""<!DOCTYPE html>
<html><head><title>Box Labels</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 20px; }}
.label {{ border: 2px solid #333; border-radius: 8px; padding: 16px 20px;
          margin: 10px; display: inline-block; min-width: 250px; page-break-inside: avoid; }}
.label h2 {{ margin: 0 0 8px 0; font-size: 1.3em; }}
.label p {{ margin: 2px 0; font-size: 0.9em; color: #555; }}
.label .count {{ font-size: 1.1em; font-weight: bold; }}
@media print {{ body {{ margin: 0; }} .no-print {{ display: none; }} }}
</style></head><body>
<div class="no-print" style="margin-bottom:16px">
<button onclick="window.print()" style="padding:8px 16px;font-size:1em">Print Labels</button>
<a href="/" style="margin-left:16px">Back to app</a></div>
"""
        for b in summary:
            name = 'Unassigned' if b['box_name'] == '__unassigned__' else b['box_name']
            val = f"${b['total_value']:.2f}" if b['total_value'] else '$0.00'
            html += f"""<div class="label">
<h2>{name}</h2>
<p class="count">{b['total_cards']} cards ({b['unique_cards']} unique)</p>
<p>Value: {val}</p>
<p>Printed: {date_str}</p>
</div>\n"""
        html += '</body></html>'
        return Response(html, mimetype='text/html')
    finally:
        conn.close()


@app.route('/api/collection/boxes')
def api_collection_boxes():
    import collection_db
    conn = collection_db.get_connection()
    try:
        boxes = collection_db.get_boxes(conn)
        return jsonify({'boxes': boxes})
    finally:
        conn.close()


@app.route('/api/collection/boxes/summary')
def api_collection_box_summary():
    import collection_db
    conn = collection_db.get_connection()
    try:
        summary = collection_db.get_box_summary(conn)
        return jsonify({'boxes': summary})
    finally:
        conn.close()


@app.route('/api/collection/inventory/<int:item_id>/box', methods=['PUT'])
def api_collection_assign_box(item_id):
    import collection_db
    data = request.json or {}
    box_name = data.get('box', '')
    move_quantity = data.get('quantity')
    if move_quantity is not None:
        move_quantity = int(move_quantity)

    conn = collection_db.get_connection()
    try:
        ok = collection_db.assign_box(conn, item_id, box_name, move_quantity)
        if not ok:
            return jsonify({'error': 'Item not found'}), 404
        return jsonify({'assigned': True})
    finally:
        conn.close()


@app.route('/api/collection/locate')
def api_collection_locate():
    """Physical locator (Phase 2.11).

    Query params:
      q: Scryfall-like query string (same syntax as sort bin queries).

    Returns JSON:
      { "query": "<q>",
        "total_cards": N,
        "groups": [ {box_name, divider_label, divider_id, box_id,
                     count, unique_cards, oracle_ids}, ... ] }

    Empty/whitespace `q` returns 400 to match the convention used by
    /api/sort/validate-query and /api/collection/inventory POST.
    """
    import collection_db
    query_str = (request.args.get('q') or '').strip()
    if not query_str:
        return jsonify({'error': 'Query string (q) is required',
                        'groups': [], 'total_cards': 0}), 400

    try:
        from query_parser import parse_query, QueryParseError
        parse_query(query_str)  # Validate up front for a clean 400.
    except QueryParseError as exc:
        return jsonify({'error': f'Invalid query: {exc}',
                        'groups': [], 'total_cards': 0}), 400
    except Exception as exc:
        return jsonify({'error': f'Invalid query: {exc}',
                        'groups': [], 'total_cards': 0}), 400

    conn = collection_db.get_connection()
    try:
        groups = collection_db.locate_cards_by_query(conn, query_str)
        total = sum(g['count'] for g in groups)
        return jsonify({
            'query': query_str,
            'total_cards': total,
            'groups': groups,
        })
    finally:
        conn.close()


@app.route('/api/locator/query', methods=['POST'])
def api_locator_query():
    """Physical locator — POST variant (Phase 2.11 acceptance-criteria path).

    Request body (JSON):
      { "q": "<scryfall-style query>" }

    Returns JSON identical to GET /api/collection/locate:
      { "query": "<q>",
        "total_cards": N,
        "groups": [ {box_name, divider_label, divider_id, box_id,
                     count, unique_cards, oracle_ids}, ... ] }

    Empty or missing `q` returns 400.
    """
    import collection_db
    data = request.get_json(silent=True) or {}
    query_str = (data.get('q') or '').strip()
    if not query_str:
        return jsonify({'error': 'Query string (q) is required',
                        'groups': [], 'total_cards': 0}), 400

    try:
        from query_parser import parse_query, QueryParseError
        parse_query(query_str)  # Validate up front for a clean 400.
    except QueryParseError as exc:
        return jsonify({'error': f'Invalid query: {exc}',
                        'groups': [], 'total_cards': 0}), 400
    except Exception as exc:
        return jsonify({'error': f'Invalid query: {exc}',
                        'groups': [], 'total_cards': 0}), 400

    conn = collection_db.get_connection()
    try:
        groups = collection_db.locate_cards_by_query(conn, query_str)
        total = sum(g['count'] for g in groups)
        return jsonify({
            'query': query_str,
            'total_cards': total,
            'groups': groups,
        })
    finally:
        conn.close()


@app.route('/api/collection/boxes/manage', methods=['GET', 'POST'])
def api_collection_boxes_manage():
    """List or create first-class boxes (Phase 2.12).

    This sits alongside `/api/collection/boxes` (which returns the legacy
    free-form box-name list derived from inventory.box). The /manage
    endpoints operate on the first-class `boxes` table introduced by
    2.12 and are what the upcoming Collection-tab UI will use.
    """
    import collection_db
    conn = collection_db.get_connection()
    try:
        if request.method == 'POST':
            data = request.json or {}
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'error': 'Box name is required'}), 400
            try:
                box_id = collection_db.add_box(
                    conn, name=name,
                    capacity=data.get('capacity'),
                    notes=(data.get('notes') or '').strip() or None,
                )
            except Exception as exc:
                return jsonify({'error': str(exc)}), 400
            return jsonify({'id': box_id, 'created': True})
        else:
            return jsonify({'boxes': collection_db.list_boxes(conn)})
    finally:
        conn.close()


@app.route('/api/collection/boxes/manage/<int:box_id>',
           methods=['PUT', 'DELETE'])
def api_collection_boxes_manage_one(box_id):
    import collection_db
    conn = collection_db.get_connection()
    try:
        if request.method == 'DELETE':
            ok = collection_db.delete_box(conn, box_id)
            if not ok:
                return jsonify({'error': 'Box not found'}), 404
            return jsonify({'deleted': True})
        data = request.json or {}
        ok = collection_db.update_box(
            conn, box_id,
            name=data.get('name'),
            capacity=data.get('capacity'),
            notes=data.get('notes'),
        )
        if not ok:
            return jsonify({'error': 'Box not found'}), 404
        return jsonify({'updated': True})
    finally:
        conn.close()


@app.route('/api/collection/dividers', methods=['GET', 'POST'])
def api_collection_dividers():
    """List or create dividers (Phase 2.12)."""
    import collection_db
    conn = collection_db.get_connection()
    try:
        if request.method == 'POST':
            data = request.json or {}
            try:
                box_id = int(data.get('box_id'))
            except (TypeError, ValueError):
                return jsonify({'error': 'box_id is required'}), 400
            label = (data.get('label') or '').strip()
            if not label:
                return jsonify({'error': 'label is required'}), 400
            try:
                position = int(data.get('position', 0))
            except (TypeError, ValueError):
                return jsonify({'error': 'position must be an integer'}), 400
            try:
                div_id = collection_db.add_divider(
                    conn, box_id=box_id, label=label, position=position,
                    capacity=data.get('capacity'),
                )
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400
            return jsonify({'id': div_id, 'created': True})
        box_id = request.args.get('box_id', type=int)
        return jsonify({'dividers': collection_db.list_dividers(conn, box_id)})
    finally:
        conn.close()


@app.route('/api/collection/dividers/<int:divider_id>',
           methods=['PUT', 'DELETE'])
def api_collection_dividers_one(divider_id):
    import collection_db
    conn = collection_db.get_connection()
    try:
        if request.method == 'DELETE':
            ok = collection_db.delete_divider(conn, divider_id)
            if not ok:
                return jsonify({'error': 'Divider not found'}), 404
            return jsonify({'deleted': True})
        data = request.json or {}
        ok = collection_db.update_divider(
            conn, divider_id,
            label=data.get('label'),
            position=data.get('position'),
            capacity=data.get('capacity'),
        )
        if not ok:
            return jsonify({'error': 'Divider not found'}), 404
        return jsonify({'updated': True})
    finally:
        conn.close()


@app.route('/api/collection/import/csv', methods=['POST'])
def api_collection_import_csv():
    import collection_db
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file selected'}), 400
    try:
        csv_text = f.read().decode('utf-8')
    except UnicodeDecodeError:
        return jsonify({'error': 'File must be UTF-8 encoded CSV'}), 400

    conn = collection_db.get_connection()
    try:
        imported, updated, skipped = collection_db.import_inventory_csv(
            conn, csv_text)
        return jsonify({
            'imported': imported,
            'updated': updated,
            'skipped': skipped,
            'total': imported + updated,
        })
    finally:
        conn.close()


@app.route('/api/collection/cull-candidates')
def api_collection_cull_candidates():
    """Return dead-weight cull candidates from the owned collection.

    Query parameters:
        max_price    float   Maximum market price to include (default 1.0)
        max_buylist  float   Maximum CK buylist price to include (default 0.05)
        min_qty      int     Minimum quantity owned (default 1)
        preset       str     "default" | "strict" (strict = price must be 0)
        staples      str     "exclude" (default) | "include"
    """
    import collection_db
    max_price = request.args.get('max_price', 1.0, type=float)
    max_buylist = request.args.get('max_buylist', 0.05, type=float)
    min_qty = request.args.get('min_qty', 1, type=int)
    preset = request.args.get('preset', 'default')
    if preset not in ('default', 'strict'):
        preset = 'default'
    exclude_staples = request.args.get('staples', 'exclude') != 'include'

    conn = collection_db.get_connection()
    try:
        candidates = collection_db.get_cull_candidates(
            conn,
            max_price=max_price,
            max_buylist_price=max_buylist,
            min_quantity=min_qty,
            preset=preset,
            exclude_staples=exclude_staples,
        )
        return jsonify({
            'candidates': candidates,
            'total': len(candidates),
            'max_price': max_price,
            'preset': preset,
        })
    finally:
        conn.close()


@app.route('/api/collection/cull-candidates/export')
def api_collection_cull_candidates_export():
    """Export cull candidates as CSV.

    Query parameters: same as GET /api/collection/cull-candidates.
    CSV columns: oracle_id, name, set_code, type_line, price_usd, quantity,
                 buylist_price, salt_score, is_universal_staple,
                 is_archetype_staple, is_cedh_staple, commander_popularity,
                 suggested_action, location, cull_reasons
    """
    import collection_db
    import csv
    import io
    max_price = request.args.get('max_price', 1.0, type=float)
    max_buylist = request.args.get('max_buylist', 0.05, type=float)
    min_qty = request.args.get('min_qty', 1, type=int)
    preset = request.args.get('preset', 'default')
    if preset not in ('default', 'strict'):
        preset = 'default'
    exclude_staples = request.args.get('staples', 'exclude') != 'include'

    conn = collection_db.get_connection()
    try:
        candidates = collection_db.get_cull_candidates(
            conn,
            max_price=max_price,
            max_buylist_price=max_buylist,
            min_quantity=min_qty,
            preset=preset,
            exclude_staples=exclude_staples,
        )
        output = io.StringIO()
        fields = [
            'oracle_id', 'name', 'set_code', 'type_line',
            'price_usd', 'quantity',
            'buylist_price', 'salt_score',
            'is_universal_staple', 'is_archetype_staple', 'is_cedh_staple',
            'commander_popularity',
            'suggested_action', 'keep_confidence',
            'location', 'cull_reasons',
        ]
        writer = csv.DictWriter(output, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for c in candidates:
            row = dict(c)
            row['cull_reasons'] = ', '.join(row.get('cull_reasons') or [])
            writer.writerow(row)
        from flask import Response
        return Response(
            output.getvalue(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=cull_candidates.csv'},
        )
    finally:
        conn.close()


# =========================================================================
# Review Queue API — card crop images, diagnostics, lookup
# =========================================================================

def _resolve_session_dir_name(start_time):
    """Convert ISO session start_time to the session_YYYYMMDD_HHMMSS dir name."""
    if not start_time:
        return None
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(start_time)
        return f"session_{dt.strftime('%Y%m%d_%H%M%S')}"
    except Exception:
        return None


def _resolve_crop_url(scan_item):
    """
    Build the URL to serve the card crop image for a scan_history row.
    Returns None if the image doesn't exist on disk.
    """
    dir_name = _resolve_session_dir_name(scan_item.get('session_start_time', ''))
    scan_num = scan_item.get('scan_num', 0)
    if not dir_name or not scan_num:
        return None
    crop_path = os.path.join(
        SCAN_LOGS_DIR, dir_name, "card_crops", f"card_{scan_num:04d}.jpg")
    if os.path.exists(crop_path):
        return f"/api/review/crop/{dir_name}/{scan_num}"
    return None


def _resolve_scan_url(scan_item):
    """
    Build the URL to serve the full camera scan image for a scan_history row.
    Returns None if the image doesn't exist on disk.
    """
    dir_name = _resolve_session_dir_name(scan_item.get('session_start_time', ''))
    scan_num = scan_item.get('scan_num', 0)
    if not dir_name or not scan_num:
        return None
    scan_path = os.path.join(
        SCAN_LOGS_DIR, dir_name, "scan_images", f"scan_{scan_num:04d}.jpg")
    if os.path.exists(scan_path):
        return f"/api/review/scan/{dir_name}/{scan_num}"
    return None


def _resolve_diagnostics_url(scan_item):
    """
    Build the URL to serve hash diagnostics for a scan_history row.
    Returns None if diagnostics don't exist on disk.
    """
    dir_name = _resolve_session_dir_name(scan_item.get('session_start_time', ''))
    scan_num = scan_item.get('scan_num', 0)
    if not dir_name or not scan_num:
        return None
    diag_path = os.path.join(
        SCAN_LOGS_DIR, dir_name, "hash_diagnostics",
        f"diag_{scan_num:04d}.json")
    if os.path.exists(diag_path):
        return f"/api/review/diagnostics-file/{dir_name}/{scan_num}"
    return None


@app.route('/api/review/crop/<session_name>/<int:scan_num>')
def api_review_crop_image(session_name, scan_num):
    """Serve a card crop image for the review queue."""
    crop_path = os.path.join(
        SCAN_LOGS_DIR, session_name, "card_crops", f"card_{scan_num:04d}.jpg")
    if os.path.exists(crop_path):
        return send_file(crop_path, mimetype='image/jpeg')
    return jsonify({'error': 'Crop image not found'}), 404


@app.route('/api/review/scan/<session_name>/<int:scan_num>')
def api_review_scan_image(session_name, scan_num):
    """Serve the full camera scan image for the review queue."""
    scan_path = os.path.join(
        SCAN_LOGS_DIR, session_name, "scan_images", f"scan_{scan_num:04d}.jpg")
    if os.path.exists(scan_path):
        return send_file(scan_path, mimetype='image/jpeg')
    return jsonify({'error': 'Scan image not found'}), 404


def _update_scan_csv_row(session_id, scan_num, new_name, new_set,
                        new_collector, extra_fields=None):
    """
    Rewrite one row of a session's scans.csv with corrected identification.

    Looks up the session's start_time from the sessions table to resolve
    the session dir, then rewrites scans.csv in place so external tools
    (regression tests, mismatch analyzers) see the corrected name.

    :param session_id: sessions.id (int)
    :param scan_num: scan number within the session (int)
    :param new_name: corrected card name
    :param new_set: corrected set code
    :param new_collector: corrected collector number
    :param extra_fields: optional dict of additional fields to overwrite
        (keys must match CSV column names, e.g. 'rarity', 'price_usd')
    :return: True if the CSV was updated, False otherwise
    """
    import collection_db
    import csv as _csv
    conn = collection_db.get_connection()
    try:
        row = conn.execute(
            "SELECT start_time FROM sessions WHERE id = ?",
            (session_id,)).fetchone()
        if not row or not row['start_time']:
            return False
        dir_name = _resolve_session_dir_name(row['start_time'])
    finally:
        conn.close()

    if not dir_name:
        return False
    csv_path = os.path.join(SCAN_LOGS_DIR, dir_name, "scans.csv")
    if not os.path.exists(csv_path):
        return False

    try:
        with open(csv_path, 'r', newline='', encoding='utf-8') as f:
            reader = _csv.DictReader(f)
            fieldnames = reader.fieldnames or []
            rows = list(reader)

        target = str(scan_num)
        updated = False
        for r in rows:
            if str(r.get('scan_num', '')) == target:
                r['name'] = new_name
                if 'set' in fieldnames:
                    r['set'] = new_set or ''
                if 'collector_number' in fieldnames:
                    r['collector_number'] = new_collector or ''
                # method = manual_review so we can tell post-hoc
                if 'method' in fieldnames:
                    r['method'] = 'manual_review'
                # hash_distance = 0 (confirmed)
                if 'hash_distance' in fieldnames:
                    r['hash_distance'] = '0.00'
                # recognized = True
                if 'recognized' in fieldnames:
                    r['recognized'] = 'True'
                if extra_fields:
                    for k, v in extra_fields.items():
                        if k in fieldnames and v is not None:
                            r[k] = str(v)
                updated = True
                break

        if not updated:
            return False

        # Write atomically via temp file
        tmp_path = csv_path + '.tmp'
        with open(tmp_path, 'w', newline='', encoding='utf-8') as f:
            writer = _csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, csv_path)
        return True
    except Exception:
        logger.exception("review: Failed to update scans.csv")
        return False


@app.route('/api/review/diagnostics-file/<session_name>/<int:scan_num>')
def api_review_diagnostics_file(session_name, scan_num):
    """
    Serve saved per-channel hash diagnostics for a scan.
    These are computed at scan time and saved alongside card crops.
    """
    diag_path = os.path.join(
        SCAN_LOGS_DIR, session_name, "hash_diagnostics",
        f"diag_{scan_num:04d}.json")
    if os.path.exists(diag_path):
        return send_file(diag_path, mimetype='application/json')
    return jsonify({'error': 'Diagnostics not found'}), 404


def _scryfallImageUrl(set_code, collector_number):
    """Build a Scryfall image URL for a card."""
    if not set_code or not collector_number:
        return None
    return (f"https://api.scryfall.com/cards/"
            f"{set_code}/{collector_number}"
            f"?format=image&version=normal")


@app.route('/api/review/lookup')
def api_review_lookup():
    """
    Look up a card by name for the review queue.
    Returns card details + image URL for confirmation.
    """
    from cards import CARD_DATA_BY_ID, extract_card_info

    name = (request.args.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name required'}), 400

    name_lower = name.lower()
    matches = []
    seen_names = set()

    for cid, cd in CARD_DATA_BY_ID.items():
        cd_name = cd.get('name', '')
        if cd_name.lower() == name_lower:
            if cd_name not in seen_names:
                seen_names.add(cd_name)
            info = extract_card_info(cid)
            prices = cd.get('prices', {})
            try:
                price = float(prices.get('usd') or prices.get('usd_foil') or 0)
            except (ValueError, TypeError):
                price = 0
            matches.append({
                'name': cd_name,
                'set_code': cd.get('set', ''),
                'set_name': cd.get('set_name', ''),
                'collector_number': cd.get('collector_number', ''),
                'colors': cd.get('colors', []),
                'type_line': cd.get('type_line', ''),
                'rarity': cd.get('rarity', ''),
                'price_usd': price,
                'image_url': _scryfallImageUrl(
                    cd.get('set', ''),
                    cd.get('collector_number', '')),
            })
            if len(matches) >= 30:
                break

    # Sort by set name for easy browsing
    matches.sort(key=lambda m: m.get('set_name', ''))
    return jsonify({'name': name, 'printings': matches})


# =========================================================================
# Database Management API
# =========================================================================

@app.route('/api/database/status')
def api_database_status():
    info = db_updater.get_current_db_info()
    info['update_status'] = db_updater.get_status()
    return jsonify(info)


@app.route('/api/database/check-update', methods=['POST'])
def api_database_check_update():
    result = db_updater.check_for_update()
    return jsonify(result)


@app.route('/api/database/update', methods=['POST'])
def api_database_update():
    ok = db_updater.start_update()
    return jsonify({'started': ok})


@app.route('/api/database/refresh-prices', methods=['POST'])
def api_database_refresh_prices():
    """
    Fast prices-only refresh: download latest bulk data JSON,
    rebuild printings map, update config, reload card data in
    place. Skips image download and hash DB regeneration.
    Takes about a minute vs 30+ for a full update.
    """
    ok = db_updater.start_prices_refresh()
    return jsonify({'started': ok})


@app.route('/api/database/update/stop', methods=['POST'])
def api_database_update_stop():
    db_updater.stop()
    return jsonify({'stopped': True})


# =========================================================================
# ArUco Calibration API
# =========================================================================

def _ensure_hardware_ready():
    """
    Helper for hardware-requiring endpoints. Returns None if the machine
    is connected and in a runnable state, otherwise returns a (response,
    status_code) tuple the caller can return directly.

    This centralizes the 'must be connected' guard so every hardware
    endpoint gives the same clear error instead of silently queuing a
    command that the worker will immediately reject.
    """
    import gcode_control
    if not gcode_control.is_connected():
        return (jsonify({
            'error': 'not_connected',
            'message': ('Machine is not connected. Click "Connect" on the '
                        'Dashboard before running hardware operations.'),
        }), 409)
    if worker.state == 'estopped':
        return (jsonify({
            'error': 'estopped',
            'message': ('Machine is in E-STOP state. Reset & re-home first.'),
        }), 409)
    if worker.state in ('sorting', 'calibrating'):
        return (jsonify({
            'error': 'busy',
            'state': worker.state,
            'message': f'Machine is busy ({worker.state}).',
        }), 409)
    return None


@app.route('/api/calibration/start', methods=['POST'])
def api_calibration_start():
    """Start ArUco bin calibration sweep."""
    guard = _ensure_hardware_ready()
    if guard is not None:
        return guard

    data = request.json or {}
    expected_sources = data.get('expected_sources', 1)
    expected_dests = data.get('expected_dests', 10)
    expect_staging = data.get('expect_staging', True)
    max_sweep_x = data.get('max_sweep_x')

    if not camera.is_active:
        camera.start()
        import time
        time.sleep(1)

    worker.enqueue('run_calibration',
                    camera=camera,
                    expected_sources=expected_sources,
                    expected_dests=expected_dests,
                    expect_staging=expect_staging,
                    max_sweep_x=max_sweep_x)
    return jsonify({'queued': True})


@app.route('/api/calibration/cancel', methods=['POST'])
def api_calibration_cancel():
    """
    Cancel a running calibration sweep.

    Crucial: this must NOT go through the worker queue. The worker thread
    is busy running the sweep, so any queued cancel would only execute
    AFTER the sweep finishes, defeating the whole point. Instead we set
    the calibrator's cancel flag directly (it's just a bool) and also
    call the worker's abort helper so _cmd_new_hardware_setup knows to
    stop before proceeding into the probe phase.
    """
    try:
        from web_calibration import calibrator
        calibrator.cancel()
    except Exception as e:
        logger.warning(f"calibrator.cancel() error: {e}")
    try:
        worker.request_abort('calibration cancelled via API')
    except Exception as e:
        logger.warning(f"worker.request_abort() error: {e}")
    return jsonify({'cancelled': True})


@app.route('/api/calibration/new-hardware-setup', methods=['POST'])
def api_new_hardware_setup():
    """
    Full 'fresh install' routine: clear cached state, home, ArUco sweep,
    probe every destination bin. Run this any time the physical bin layout
    changes (new bins, repositioned trays, rebuilt X carriage, etc.).
    """
    guard = _ensure_hardware_ready()
    if guard is not None:
        return guard

    data = request.json or {}
    expected_sources = data.get('expected_sources', 1)
    expected_dests = data.get('expected_dests', 10)
    expect_staging = data.get('expect_staging', True)
    max_sweep_x = data.get('max_sweep_x')

    if not camera.is_active:
        camera.start()
        import time
        time.sleep(1)

    worker.enqueue('new_hardware_setup',
                    camera=camera,
                    expected_sources=expected_sources,
                    expected_dests=expected_dests,
                    expect_staging=expect_staging,
                    max_sweep_x=max_sweep_x)
    return jsonify({'queued': True})


# =========================================================================
# Drop-height tuner API
# =========================================================================
# Standalone interactive workflow for dialing in Z_DROP_OFFSET. Runs as
# a small state machine in the worker (see _cmd_drop_tuner_* in
# web_worker.py). The user can step Z up/down and test-drop a card
# before saving the height to drop_height.json.
# =========================================================================


@app.route('/api/calibration/drop-tuner/start', methods=['POST'])
def api_drop_tuner_start():
    guard = _ensure_hardware_ready()
    if guard is not None:
        return guard
    data = request.json or {}
    bin_number = data.get('bin_number')
    kwargs = {}
    if bin_number is not None:
        try:
            kwargs['bin_number'] = int(bin_number)
        except (TypeError, ValueError):
            return jsonify({'error': 'bad_bin_number'}), 400
    worker.enqueue('drop_tuner_start', **kwargs)
    return jsonify({'queued': True})


@app.route('/api/calibration/drop-tuner/pickup', methods=['POST'])
def api_drop_tuner_pickup():
    worker.enqueue('drop_tuner_pickup')
    return jsonify({'queued': True})


@app.route('/api/calibration/drop-tuner/step-z', methods=['POST'])
def api_drop_tuner_step_z():
    data = request.json or {}
    delta = data.get('delta_mm')
    try:
        delta_f = float(delta)
    except (TypeError, ValueError):
        return jsonify({'error': 'bad_delta_mm'}), 400
    worker.enqueue('drop_tuner_step_z', delta_mm=delta_f)
    return jsonify({'queued': True})


@app.route('/api/calibration/drop-tuner/test-drop', methods=['POST'])
def api_drop_tuner_test_drop():
    worker.enqueue('drop_tuner_test_drop')
    return jsonify({'queued': True})


@app.route('/api/calibration/drop-tuner/save', methods=['POST'])
def api_drop_tuner_save():
    worker.enqueue('drop_tuner_save')
    return jsonify({'queued': True})


@app.route('/api/calibration/drop-tuner/cancel', methods=['POST'])
def api_drop_tuner_cancel():
    worker.enqueue('drop_tuner_cancel')
    return jsonify({'queued': True})


@app.route('/api/calibration/drop-tuner/status', methods=['GET'])
def api_drop_tuner_status():
    """Return current Z_DROP_OFFSET so the UI can display it on load."""
    import gcode_control
    return jsonify({
        'current_offset': gcode_control.Z_DROP_OFFSET,
        'z_max': gcode_control.Z_MAX,
        'active': worker._drop_tuner is not None,
        'phase': (worker._drop_tuner or {}).get('phase'),
    })


@app.route('/api/calibration/last-setup', methods=['GET'])
def api_calibration_last_setup_get():
    """Return the auto-saved last hardware setup, if one exists."""
    import json
    path = worker._last_setup_path
    if not os.path.exists(path):
        return jsonify({'exists': False})
    try:
        with open(path, 'r') as f:
            payload = json.load(f)
        return jsonify({'exists': True, 'setup': payload})
    except Exception as e:
        return jsonify({'exists': False, 'error': str(e)}), 500


@app.route('/api/calibration/last-setup/reload', methods=['POST'])
def api_calibration_last_setup_reload():
    """Re-apply the auto-saved last setup to the live machine state.

    Useful if the user tweaked something at runtime and wants to get
    back to the saved calibration without re-running the full sweep.
    """
    guard = _ensure_hardware_ready()
    if guard is not None:
        return guard
    try:
        summary = worker.load_last_setup()
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if summary is None:
        return jsonify({'error': 'no_saved_setup',
                        'message': 'No saved hardware setup on disk.'}), 404
    return jsonify({'reloaded': True, 'summary': summary})


@app.route('/api/calibration/last-setup/save-as', methods=['POST'])
def api_calibration_last_setup_save_as():
    """Copy the last auto-saved setup to a named file the user can keep.

    Saved copies live alongside _last_setup.json as `setup_<name>.json`
    so they don't collide with the bin_configs directory used by the
    old bin-config save flow.
    """
    import json
    data = request.json or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'name_required',
                        'message': 'Give the saved setup a name.'}), 400
    # Sanitize the filename
    safe = _sanitize_setup_name(name)
    if not safe:
        return jsonify({'error': 'bad_name',
                        'message': 'Invalid characters in name.'}), 400

    src = worker._last_setup_path
    if not os.path.exists(src):
        return jsonify({'error': 'no_setup',
                        'message': 'Run a hardware setup first.'}), 404

    dst_dir = os.path.dirname(src)
    dst = os.path.join(dst_dir, f'setup_{safe}.json')
    try:
        with open(src, 'r') as f:
            payload = json.load(f)
        payload['name'] = name
        with open(dst, 'w') as f:
            json.dump(payload, f, indent=2)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'saved': True, 'filename': os.path.basename(dst)})


def _sanitize_setup_name(name):
    """Strip a user-supplied setup name down to a safe filename stem.

    Only alphanumerics, space, dash, underscore, and dot survive. This
    is shared between save-as and load so both endpoints agree on what
    `setup_<name>.json` looks like on disk.
    """
    return ''.join(
        c for c in (name or '') if c.isalnum() or c in '-_. '
    ).strip()


def _saved_setups_dir():
    """Directory where named setup files live (same dir as auto-save)."""
    return os.path.dirname(worker._last_setup_path)


@app.route('/api/calibration/last-setup/list', methods=['GET'])
def api_calibration_last_setup_list():
    """Return metadata for every `setup_<name>.json` file on disk.

    Used by the calibration tab to populate the 'Load saved setup'
    dropdown. Lightweight — only reads the top-level fields, no bin
    position decoding.
    """
    import json
    dst_dir = _saved_setups_dir()
    setups = []
    if os.path.isdir(dst_dir):
        for fname in sorted(os.listdir(dst_dir)):
            if not fname.startswith('setup_') or not fname.endswith('.json'):
                continue
            fpath = os.path.join(dst_dir, fname)
            try:
                with open(fpath, 'r') as f:
                    payload = json.load(f)
                setups.append({
                    'filename': fname,
                    'name': payload.get('name') or fname[6:-5],
                    'saved_at': payload.get('saved_at'),
                    'dest_bin_count': payload.get('dest_bin_count'),
                    'source_bin_count': payload.get('source_bin_count'),
                })
            except Exception as e:
                # Corrupt file: include it with an error flag rather
                # than hiding it, so the user can delete it from the UI.
                setups.append({
                    'filename': fname,
                    'name': fname[6:-5],
                    'error': str(e),
                })
    return jsonify({'setups': setups})


@app.route('/api/calibration/last-setup/load', methods=['POST'])
def api_calibration_last_setup_load():
    """Load a named setup file and apply it to the live machine state.

    Body: {"name": "<saved name>"}  OR  {"filename": "setup_<safe>.json"}

    Mirrors /reload but lets the UI pick from the list of saved
    setups instead of always loading the auto-save. Same hardware
    readiness guard so we don't try to push positions to a
    disconnected board.
    """
    guard = _ensure_hardware_ready()
    if guard is not None:
        return guard

    data = request.json or {}
    filename = (data.get('filename') or '').strip()
    name = (data.get('name') or '').strip()

    # Resolve the target file. Accept either a pre-computed filename
    # from the list endpoint or a user-visible name.
    if filename:
        # Only allow the setup_*.json shape — no path traversal.
        base = os.path.basename(filename)
        if not base.startswith('setup_') or not base.endswith('.json'):
            return jsonify({'error': 'bad_filename',
                            'message': 'Filename must be setup_<name>.json.'}), 400
        path = os.path.join(_saved_setups_dir(), base)
    elif name:
        safe = _sanitize_setup_name(name)
        if not safe:
            return jsonify({'error': 'bad_name',
                            'message': 'Invalid characters in name.'}), 400
        path = os.path.join(_saved_setups_dir(), f'setup_{safe}.json')
    else:
        return jsonify({'error': 'name_required',
                        'message': 'Provide a name or filename.'}), 400

    if not os.path.exists(path):
        return jsonify({'error': 'not_found',
                        'message': f'No saved setup at {os.path.basename(path)}.'}), 404

    try:
        summary = worker.load_last_setup(path=path)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    if summary is None:
        return jsonify({'error': 'load_failed',
                        'message': 'Setup file could not be parsed.'}), 500
    return jsonify({
        'loaded': True,
        'filename': os.path.basename(path),
        'summary': summary,
    })


@app.route('/api/calibration/last-setup/delete', methods=['POST'])
def api_calibration_last_setup_delete():
    """Delete a named setup file."""
    data = request.json or {}
    filename = (data.get('filename') or '').strip()
    base = os.path.basename(filename)
    if not base.startswith('setup_') or not base.endswith('.json'):
        return jsonify({'error': 'bad_filename'}), 400
    path = os.path.join(_saved_setups_dir(), base)
    if not os.path.exists(path):
        return jsonify({'error': 'not_found'}), 404
    try:
        os.remove(path)
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'deleted': True, 'filename': base})


@app.route('/api/calibration/retry', methods=['POST'])
def api_calibration_retry():
    """Re-run the new hardware setup with the same params as last time.

    Convenience wrapper — the calibration tab's Retry button calls this
    so the user doesn't have to re-type bin counts if they just want
    another pass at the sweep.
    """
    guard = _ensure_hardware_ready()
    if guard is not None:
        return guard

    data = request.json or {}
    expected_sources = data.get('expected_sources', 1)
    expected_dests = data.get('expected_dests', 10)
    expect_staging = data.get('expect_staging', True)
    max_sweep_x = data.get('max_sweep_x')

    if not camera.is_active:
        camera.start()
        import time
        time.sleep(1)

    worker.enqueue('new_hardware_setup',
                    camera=camera,
                    expected_sources=expected_sources,
                    expected_dests=expected_dests,
                    expect_staging=expect_staging,
                    max_sweep_x=max_sweep_x)
    return jsonify({'queued': True, 'retry': True})


@app.route('/api/calibration/status')
def api_calibration_status():
    """
    Combined calibration status — both the ArUco sweep-in-progress state
    and the per-step "is this step already satisfied" booleans used by
    the calibration wizard (Phase 4 item 4.22).

    The old ``calibrator.get_status()`` shape (running/progress/message/
    bins_found/discovered) is preserved for backward compatibility. The
    wizard consumes the new booleans under ``wizard``:

        wizard.machine_connected    — serial port open
        wizard.camera_connected     — camera device active
        wizard.bin_x_calibrated     — ArUco sweep has run + saved bin X positions
        wizard.bin_z_probed         — every bin has a probe_results entry
        wizard.staging_roi_set      — staging_roi.json exists
        wizard.focus_locked         — camera has locked its autofocus

    Top-level booleans are also mirrored so lightweight consumers can
    read e.g. ``machine_connected`` directly without digging in.
    """
    status = calibrator.get_status()
    if calibrator.calibration_result:
        status['result'] = calibrator.calibration_result

    wizard = _wizard_step_status()
    status['wizard'] = wizard
    # Mirror flat — handy for quick probes and simpler tests.
    for k, v in wizard.items():
        status.setdefault(k, v)
    return jsonify(status)


def _wizard_step_status():
    """
    Inspect live state + on-disk calibration artifacts and return the
    per-step satisfaction booleans for the calibration wizard.

    Kept as a plain function (not a method on any class) so unit tests
    can patch its dependencies cleanly.
    """
    import gcode_control
    from config import STAGING_ROI_PATH

    # Step 1: machine connected
    try:
        machine_connected = bool(gcode_control.is_connected())
    except Exception:
        machine_connected = False

    # Step 2: camera connected
    try:
        camera_connected = bool(camera.is_active)
    except Exception:
        camera_connected = False

    # Steps 4 + 5: bin X calibrated + bin Z probed. `_last_setup.json`
    # is auto-written at the end of a full ArUco sweep + probe pass and
    # is the authoritative "we've done this" marker. Probe results are
    # only populated once every destination bin has been touched down.
    bin_x_calibrated = False
    bin_z_probed = False
    try:
        last_setup_path = getattr(worker, '_last_setup_path', None)
        if last_setup_path and os.path.exists(last_setup_path):
            with open(last_setup_path, 'r') as f:
                setup = json.load(f) or {}
            locs = setup.get('locations') or {}
            # At least one destination bin (>0) must be recorded. Bin 0
            # is the source bin, so we ignore it for "calibrated".
            dest_locs = [k for k in locs.keys() if str(k) != '0']
            bin_x_calibrated = bool(dest_locs)
            probes = setup.get('probe_results') or {}
            probed_dests = [k for k in probes.keys() if str(k) != '0']
            bin_z_probed = bin_x_calibrated and len(probed_dests) >= len(dest_locs)
    except Exception:
        pass

    # Step 6: staging ROI — the JSON file is written by save_staging_roi().
    try:
        staging_roi_set = os.path.exists(STAGING_ROI_PATH)
    except Exception:
        staging_roi_set = False

    # Step 7: focus locked — camera exposes this as a live attribute.
    try:
        focus_locked = bool(getattr(camera, 'focus_locked', False))
    except Exception:
        focus_locked = False

    return {
        'machine_connected': machine_connected,
        'camera_connected': camera_connected,
        'bin_x_calibrated': bin_x_calibrated,
        'bin_z_probed': bin_z_probed,
        'staging_roi_set': staging_roi_set,
        'focus_locked': focus_locked,
    }


@app.route('/api/calibration/wizard/lock-focus', methods=['POST'])
def api_wizard_lock_focus():
    """
    Standalone focus-lock endpoint used by the calibration wizard.

    The existing ``/api/session/confirm-focus`` only works mid-session
    (it releases a worker-side Event waited on by the sort loop). The
    wizard needs to lock focus in isolation, before any session exists,
    so we call ``camera.lock_focus()`` directly. The camera must be
    running for the underlying cv2.VideoCapture property write to take
    effect.
    """
    if not camera.is_active:
        camera.start()
        import time
        time.sleep(0.5)
    try:
        camera.lock_focus()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500
    return jsonify({'ok': camera.focus_locked, 'focus_locked': camera.focus_locked})


@app.route('/api/calibration/detect-markers', methods=['POST'])
def api_calibration_detect_markers():
    """Detect ArUco markers in the current camera frame (one-shot)."""
    if not camera.is_active:
        camera.start()
        import time
        time.sleep(1)

    frame = camera.get_frame()
    if frame is None:
        return jsonify({'error': 'No camera frame available'}), 400

    markers = calibrator.detect_markers(frame)
    result = []
    for m in markers:
        result.append({
            'id': m['id'],
            'type': calibrator.get_marker_type(m['id']),
            'center_x': m['center'][0],
            'center_y': m['center'][1],
        })

    # Also generate an annotated frame for preview
    annotated = calibrator.draw_markers_on_frame(frame, markers)
    if annotated is not None:
        ret, jpeg = cv2.imencode('.jpg', annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if ret:
            import base64
            img_b64 = base64.b64encode(jpeg.tobytes()).decode('utf-8')
            return jsonify({
                'markers': result,
                'frame': f'data:image/jpeg;base64,{img_b64}',
            })

    return jsonify({'markers': result})


@app.route('/api/calibration/check-empty', methods=['POST'])
def api_calibration_check_empty():
    """Check if source bins are empty using ArUco detection."""
    if not camera.is_active:
        camera.start()
        import time
        time.sleep(1)

    worker.enqueue('check_source_empty', camera=camera)
    return jsonify({'queued': True})


@app.route('/api/calibration/source-bins')
def api_calibration_source_bins():
    """Get information about calibrated source bins."""
    return jsonify(worker.get_source_bins_info())


@app.route('/api/calibration/offset', methods=['GET', 'POST'])
def api_calibration_offset():
    """Get or set the camera X offset."""
    if request.method == 'POST':
        data = request.json or {}
        offset = data.get('offset')
        if offset is not None:
            calibrator.camera_x_offset = float(offset)
            return jsonify({'offset': calibrator.camera_x_offset, 'updated': True})
        return jsonify({'error': 'Provide offset value'}), 400
    return jsonify({'offset': calibrator.camera_x_offset})


@app.route('/api/calibration/max-sweep', methods=['GET', 'POST'])
def api_calibration_max_sweep():
    """Get or set the maximum sweep distance."""
    if request.method == 'POST':
        data = request.json or {}
        max_x = data.get('max_sweep_x')
        if max_x is not None:
            calibrator.max_sweep_x = float(max_x)
            return jsonify({'max_sweep_x': calibrator.max_sweep_x, 'updated': True})
        return jsonify({'error': 'Provide max_sweep_x value'}), 400
    return jsonify({'max_sweep_x': calibrator.max_sweep_x})


@app.route('/api/calibration/generate-markers')
def api_calibration_generate_markers():
    """Generate printable ArUco marker images as an HTML page."""
    marker_ids = request.args.getlist('ids', type=int)
    if not marker_ids:
        # Default: 2 source (0, 1) + 10 destination (10-19)
        marker_ids = [0, 1] + list(range(10, 20))

    marker_size = int(request.args.get('size', 200))  # pixels
    aruco_dict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)

    import base64
    html = """<!DOCTYPE html>
<html><head><title>ArUco Markers for Bin Calibration</title>
<style>
body { font-family: Arial, sans-serif; margin: 20px; }
.marker-card { display: inline-block; margin: 10px; text-align: center;
               border: 1px solid #ccc; padding: 10px; border-radius: 8px;
               page-break-inside: avoid; }
.marker-card img { display: block; margin: 0 auto 8px; }
.marker-card .label { font-weight: bold; font-size: 1.1em; }
.marker-card .type { font-size: 0.9em; color: #666; }
.instructions { max-width: 600px; margin: 0 auto 20px; padding: 15px;
                background: #f5f5f5; border-radius: 8px; }
@media print { .no-print { display: none; } body { margin: 0; } }
</style></head><body>
<div class="no-print instructions">
<h2>ArUco Markers for Bin Calibration</h2>
<p><strong>Print at 100% scale.</strong> Cut out each marker and tape to the bottom of the corresponding bin.</p>
<p>IDs 0-9 = Source bins (to be sorted) | IDs 10-49 = Destination bins</p>
<button onclick="window.print()" style="padding:8px 16px;font-size:1em">Print Markers</button>
<a href="/" style="margin-left:16px">Back to app</a>
</div>
"""

    for mid in marker_ids:
        marker_img = cv2.aruco.generateImageMarker(aruco_dict, mid, marker_size)
        ret, png = cv2.imencode('.png', marker_img)
        if ret:
            b64 = base64.b64encode(png.tobytes()).decode('utf-8')
            mtype = 'SOURCE' if mid < 10 else 'DESTINATION'
            html += f"""<div class="marker-card">
<img src="data:image/png;base64,{b64}" width="{marker_size}" height="{marker_size}">
<div class="label">ID {mid}</div>
<div class="type">{mtype}</div>
</div>\n"""

    html += '</body></html>'
    return Response(html, mimetype='text/html')


# =========================================================================
# Scan Confidence Review Queue API
# =========================================================================

@app.route('/api/collection/review-queue')
def api_review_queue():
    """Get scans with borderline hash distances for manual review."""
    import collection_db
    threshold = float(request.args.get('threshold', 12.0))
    limit = int(request.args.get('limit', 100))
    session_id = request.args.get('session_id')
    include_unrecognized = request.args.get('unrecognized', '1') == '1'

    conn = collection_db.get_connection()
    try:
        conditions = []
        params = []

        if include_unrecognized:
            # Borderline recognized OR unrecognized. Prefix every column with
            # sh. because the JOIN against `sessions` introduces an ambiguous
            # `recognized` column otherwise (sqlite raises OperationalError).
            conditions.append(
                "((sh.recognized = 1 AND sh.hash_distance IS NOT NULL AND sh.hash_distance >= ?) "
                "OR sh.recognized = 0)")
            params.append(threshold)
        else:
            conditions.append(
                "sh.recognized = 1 AND sh.hash_distance IS NOT NULL AND sh.hash_distance >= ?")
            params.append(threshold)

        if session_id:
            conditions.append("sh.session_id = ?")
            params.append(int(session_id))

        where = " AND ".join(conditions) if conditions else "1=1"

        cursor = conn.execute(f"""
            SELECT sh.id, sh.session_id, sh.scan_num, sh.timestamp,
                   sh.name, sh.set_code, sh.collector_number,
                   sh.colors, sh.type_line, sh.rarity, sh.price_usd,
                   sh.bin, sh.method, sh.hash_distance, sh.recognized,
                   s.sort_mode, s.notes as session_notes,
                   s.start_time as session_start_time
            FROM scan_history sh
            LEFT JOIN sessions s ON sh.session_id = s.id
            WHERE {where}
            ORDER BY sh.recognized ASC, sh.hash_distance DESC, sh.timestamp DESC
            LIMIT ?
        """, params + [limit])

        rows = cursor.fetchall()
        items = []
        for r in rows:
            item = {
                'id': r['id'],
                'session_id': r['session_id'],
                'scan_num': r['scan_num'],
                'timestamp': r['timestamp'],
                'name': r['name'],
                'set_code': r['set_code'],
                'collector_number': r['collector_number'],
                'colors': r['colors'],
                'type_line': r['type_line'],
                'rarity': r['rarity'],
                'price_usd': r['price_usd'],
                'bin': r['bin'],
                'method': r['method'],
                'hash_distance': r['hash_distance'],
                'recognized': bool(r['recognized']),
                'sort_mode': r['sort_mode'],
                'session_notes': r['session_notes'],
            }
            # Add card crop image URL and diagnostics URL
            scan_ref = {
                'session_start_time': r['session_start_time'],
                'scan_num': r['scan_num'],
            }
            item['crop_url'] = _resolve_crop_url(scan_ref)
            item['scan_url'] = _resolve_scan_url(scan_ref)
            item['diagnostics_url'] = _resolve_diagnostics_url(scan_ref)
            items.append(item)

        return jsonify({'items': items, 'threshold': threshold})
    finally:
        conn.close()


@app.route('/api/collection/review-queue/<int:scan_id>/confirm', methods=['POST'])
def api_review_confirm(scan_id):
    """Confirm a borderline scan is correct — no action needed."""
    import collection_db
    conn = collection_db.get_connection()
    try:
        # Mark as confirmed by setting hash_distance to 0 (below any threshold)
        conn.execute(
            "UPDATE scan_history SET hash_distance = 0 WHERE id = ?",
            (scan_id,))
        conn.commit()
        return jsonify({'confirmed': True})
    finally:
        conn.close()


@app.route('/api/collection/review-queue/<int:scan_id>/correct', methods=['POST'])
def api_review_correct(scan_id):
    """Correct a misidentified scan: update scan_history and inventory."""
    import collection_db
    data = request.json or {}
    new_name = data.get('name', '').strip()
    new_set = data.get('set_code', '').strip()
    new_collector = data.get('collector_number', '').strip()

    if not new_name:
        return jsonify({'error': 'Card name required'}), 400

    conn = collection_db.get_connection()
    try:
        # Get the old scan record
        old = conn.execute(
            "SELECT name, set_code, collector_number, recognized, session_id "
            "FROM scan_history WHERE id = ?",
            (scan_id,)).fetchone()
        if not old:
            return jsonify({'error': 'Scan not found'}), 404

        old_name = old['name']
        old_set = old['set_code']
        old_collector = old['collector_number']
        was_unrecognized = not old['recognized']

        # Update scan_history
        conn.execute(
            "UPDATE scan_history SET name=?, set_code=?, collector_number=?, "
            "recognized=1, method='manual_review', hash_distance=0 WHERE id=?",
            (new_name, new_set, new_collector, scan_id))

        # Update session stats if this was previously unrecognized
        if was_unrecognized and old['session_id']:
            conn.execute(
                """UPDATE sessions
                   SET recognized = recognized + 1,
                       unrecognized = MAX(0, unrecognized - 1)
                   WHERE id = ?""",
                (old['session_id'],))

        # Decrement old inventory entry (if it exists and was recognized)
        if old_name:
            old_inv = conn.execute(
                "SELECT id, quantity FROM inventory "
                "WHERE name=? AND set_code=? AND collector_number=?",
                (old_name, old_set, old_collector)).fetchone()
            if old_inv:
                if old_inv['quantity'] <= 1:
                    conn.execute("DELETE FROM inventory WHERE id=?",
                                 (old_inv['id'],))
                else:
                    conn.execute("UPDATE inventory SET quantity = quantity - 1 "
                                 "WHERE id=?", (old_inv['id'],))

        # Increment new inventory entry (or create)
        from datetime import datetime
        now = datetime.now().isoformat()
        existing = conn.execute(
            "SELECT id FROM inventory "
            "WHERE name=? AND set_code=? AND collector_number=?",
            (new_name, new_set, new_collector)).fetchone()
        if existing:
            conn.execute(
                "UPDATE inventory SET quantity = quantity + 1, last_scanned=? "
                "WHERE id=?", (now, existing['id']))
        else:
            # Look up card info if we have it
            colors = data.get('colors')
            type_line = data.get('type_line')
            rarity = data.get('rarity')
            price_usd = data.get('price_usd')
            conn.execute(
                "INSERT INTO inventory (name, set_code, collector_number, "
                "colors, type_line, rarity, price_usd, quantity, "
                "first_scanned, last_scanned) VALUES (?,?,?,?,?,?,?,1,?,?)",
                (new_name, new_set, new_collector, colors, type_line,
                 rarity, price_usd, now, now))

        conn.commit()

        # Also update the session's scans.csv on disk so external tools
        # (regression tests, mismatch analyzers) see the corrected name.
        session_id = old['session_id']
        scan_num = conn.execute(
            "SELECT scan_num FROM scan_history WHERE id = ?",
            (scan_id,)).fetchone()
        csv_updated = False
        if session_id and scan_num:
            csv_updated = _update_scan_csv_row(
                session_id=session_id,
                scan_num=scan_num['scan_num'],
                new_name=new_name,
                new_set=new_set,
                new_collector=new_collector,
                extra_fields={
                    'colors': data.get('colors') or '',
                    'type_line': data.get('type_line') or '',
                    'rarity': data.get('rarity') or '',
                    'price_usd': data.get('price_usd') or '',
                },
            )

        return jsonify({
            'corrected': True,
            'csv_updated': csv_updated,
            'old': {'name': old_name, 'set_code': old_set},
            'new': {'name': new_name, 'set_code': new_set},
        })
    finally:
        conn.close()


@app.route('/api/collection/review-queue/<int:scan_id>/dismiss', methods=['POST'])
def api_review_dismiss(scan_id):
    """Dismiss a scan from the review queue (remove from inventory if unrecognized)."""
    import collection_db
    conn = collection_db.get_connection()
    try:
        # Set hash_distance to 0 to remove from queue
        conn.execute(
            "UPDATE scan_history SET hash_distance = 0 WHERE id = ?",
            (scan_id,))
        conn.commit()
        return jsonify({'dismissed': True})
    finally:
        conn.close()


# =========================================================================
# Per-attribute Detection Review Queues (Phase 4 item 4.20)
# =========================================================================

@app.route('/api/detection-reviews/counts')
def api_detection_review_counts():
    """Pending + reviewed counts per variable. Used to populate tab badges."""
    import collection_db
    conn = collection_db.get_connection()
    try:
        return jsonify(collection_db.get_detection_review_counts(conn))
    finally:
        conn.close()


@app.route('/api/detection-reviews/seed', methods=['POST'])
def api_detection_reviews_seed():
    """
    Ensure detection_reviews rows exist for every scan (one per variable).
    Idempotent — safe to call repeatedly. No detector modifications; rows
    where the detector can't supply confidence surface with confidence=NULL
    (sorted first in the queue so the human reviews the most important
    cases first).
    """
    import collection_db
    body = request.get_json(silent=True) or {}
    only_recognized = bool(body.get('only_recognized', True))
    limit = body.get('limit')
    conn = collection_db.get_connection()
    try:
        inserted = collection_db.seed_detection_reviews_from_scans(
            conn, only_recognized=only_recognized, limit=limit)
        return jsonify({'inserted': inserted})
    finally:
        conn.close()


@app.route('/api/detection-reviews/<variable>')
def api_detection_reviews_list(variable):
    """
    Queue items for one variable, ordered by ascending confidence (NULLs
    first). `?include_reviewed=1` shows items that already have a verdict.
    """
    import collection_db
    if variable not in collection_db.DETECTION_VARIABLES:
        return jsonify({'error': f'unknown variable: {variable}'}), 400

    include_reviewed = request.args.get('include_reviewed', '0') == '1'
    limit = int(request.args.get('limit', 200))

    conn = collection_db.get_connection()
    try:
        rows = collection_db.list_detection_reviews(
            conn, variable,
            include_reviewed=include_reviewed, limit=limit)
        # Add crop + scan image URLs so the UI can render the card.
        for r in rows:
            scan_ref = {
                'session_start_time': r.get('session_start_time', ''),
                'scan_num': r.get('scan_num', 0),
            }
            r['crop_url'] = _resolve_crop_url(scan_ref)
            r['scan_url'] = _resolve_scan_url(scan_ref)
        return jsonify({
            'variable': variable,
            'items': rows,
            'include_reviewed': include_reviewed,
        })
    finally:
        conn.close()


@app.route('/api/detection-reviews/recent')
def api_detection_reviews_recent():
    """Detection-review queue items for the most-recently-ended session
    (or most-recently-started if none has ended cleanly). Used by the
    Sort tab's post-sort hero to surface what needs review without
    making the user switch to Setup.

    Limit param caps the response; default 20 is enough for the
    inline preview in the post-sort hero.
    """
    import collection_db
    limit = int(request.args.get('limit', 20))
    conn = collection_db.get_connection()
    try:
        rows = collection_db.list_recent_session_reviews(conn, limit=limit)
        for r in rows:
            scan_ref = {
                'session_start_time': r.get('session_start_time', ''),
                'scan_num': r.get('scan_num', 0),
            }
            r['crop_url'] = _resolve_crop_url(scan_ref)
            r['scan_url'] = _resolve_scan_url(scan_ref)
        return jsonify({'items': rows, 'count': len(rows)})
    finally:
        conn.close()


@app.route('/api/detection-reviews/<int:review_id>/verdict',
           methods=['PATCH', 'POST'])
def api_detection_review_verdict(review_id):
    """
    Record a user's verdict on a review row. Body:
      { "verdict": "correct" | "wrong" | "skip",
        "correction": "optional free-text if wrong" }
    Idempotent — repeating the same verdict refreshes reviewed_at.
    """
    import collection_db
    body = request.get_json(silent=True) or {}
    verdict = (body.get('verdict') or '').strip()
    correction = body.get('correction')
    if verdict not in collection_db.VALID_VERDICTS:
        return jsonify({
            'error': 'verdict must be one of '
                     f'{collection_db.VALID_VERDICTS}',
        }), 400

    conn = collection_db.get_connection()
    try:
        ok = collection_db.set_detection_verdict(
            conn, review_id, verdict, correction=correction)
        if not ok:
            return jsonify({'error': 'review not found'}), 404
        return jsonify({'ok': True, 'review_id': review_id,
                        'verdict': verdict})
    finally:
        conn.close()


# =========================================================================
# Price Update API
# =========================================================================

_price_update_thread = None
_price_update_status = {
    'running': False,
    'progress': 0,
    'total': 0,
    'updated': 0,
    'errors': 0,
    'message': '',
    'last_run': None,
}


def _run_price_update():
    """Background thread: update prices from the already-downloaded Scryfall bulk JSON.

    This is MUCH faster than per-card API calls — we already have the bulk data
    file locally, so we just scan it once and match against inventory.
    """
    import collection_db

    global _price_update_status
    _price_update_status = {
        'running': True, 'progress': 0, 'total': 0,
        'updated': 0, 'errors': 0, 'message': 'Loading bulk data...',
        'last_run': None,
    }
    socketio.emit('price_update_progress', dict(_price_update_status))

    conn = collection_db.get_connection()
    try:
        # Step 1: Build a lookup from the bulk JSON
        from config import CARDS_JSON_PATH
        if not os.path.exists(CARDS_JSON_PATH):
            _price_update_status['running'] = False
            _price_update_status['message'] = (
                'No bulk data file found. Download it from the Database tab first.')
            socketio.emit('price_update_progress', dict(_price_update_status))
            return

        _price_update_status['message'] = 'Loading bulk JSON (this may take a moment)...'
        socketio.emit('price_update_progress', dict(_price_update_status))

        # Build price index: (set_code, collector_number) -> price
        price_index = {}

        def _parse_price(prices):
            try:
                p = float(prices.get('usd') or prices.get('usd_foil') or 0)
                return p if p > 0 else None
            except (ValueError, TypeError):
                return None

        try:
            import ijson
            _has_ijson = True
        except ImportError:
            _has_ijson = False

        with open(CARDS_JSON_PATH, 'r', encoding='utf-8') as f:
            if _has_ijson:
                for card in ijson.items(f, 'item'):
                    if not _price_update_status['running']:
                        break
                    sc = card.get('set', '')
                    cn = card.get('collector_number', '')
                    if sc and cn:
                        price_index[(sc, cn)] = _parse_price(card.get('prices', {}))
            else:
                # ijson not available — load full JSON into memory
                _price_update_status['message'] = 'Loading full JSON into memory...'
                socketio.emit('price_update_progress', dict(_price_update_status))
                cards_data = json.load(f)
                for card in cards_data:
                    if not _price_update_status['running']:
                        break
                    sc = card.get('set', '')
                    cn = card.get('collector_number', '')
                    if sc and cn:
                        price_index[(sc, cn)] = _parse_price(card.get('prices', {}))

        if not _price_update_status['running']:
            _price_update_status['message'] = 'Cancelled'
            socketio.emit('price_update_progress', dict(_price_update_status))
            return

        _price_update_status['message'] = (
            f'Loaded {len(price_index)} card prices. Updating inventory...')
        socketio.emit('price_update_progress', dict(_price_update_status))

        # Step 2: Update inventory prices
        cursor = conn.execute(
            "SELECT id, set_code, collector_number, price_usd FROM inventory "
            "WHERE set_code != '' AND collector_number != ''")
        rows = cursor.fetchall()
        total = len(rows)
        _price_update_status['total'] = total

        updated = 0
        not_found = 0

        # Walk rows in chunks; collect (new_price, id) tuples for cards
        # whose price actually changed, then executemany per chunk. This
        # replaces what used to be one UPDATE per row (10k+ statements on
        # a real-sized inventory). Progress still emits between chunks so
        # the UI stays responsive and Cancel still works.
        UPDATE_CHUNK = 500
        pending_updates = []
        cancelled = False

        def _flush_pending():
            """Apply queued price updates as a single executemany."""
            nonlocal pending_updates
            if pending_updates:
                conn.executemany(
                    "UPDATE inventory SET price_usd=? WHERE id=?",
                    pending_updates,
                )
                pending_updates = []

        for i, row in enumerate(rows):
            if not _price_update_status['running']:
                _flush_pending()
                _price_update_status['message'] = 'Cancelled'
                cancelled = True
                break

            key = (row['set_code'], row['collector_number'])
            if key in price_index:
                new_price = price_index[key]
                old_price = row['price_usd']
                if new_price != old_price:
                    pending_updates.append((new_price, row['id']))
                    updated += 1
            else:
                not_found += 1

            # Flush + emit progress every UPDATE_CHUNK rows. Same cadence
            # as the prior per-100-row emit, but the writes are batched.
            if (i + 1) % UPDATE_CHUNK == 0 or i == total - 1:
                _flush_pending()
                _price_update_status['progress'] = i + 1
                _price_update_status['updated'] = updated
                _price_update_status['errors'] = not_found
                _price_update_status['message'] = (
                    f'{i + 1}/{total} checked, {updated} updated, '
                    f'{not_found} not in bulk data')
                socketio.emit('price_update_progress', dict(_price_update_status))

        # Belt-and-suspenders: anything left in the buffer if we somehow
        # exited the loop without hitting a flush boundary.
        if not cancelled:
            _flush_pending()
        conn.commit()

        from datetime import datetime
        _price_update_status['last_run'] = datetime.now().isoformat()
        _price_update_status['running'] = False
        _price_update_status['message'] = (
            f'Complete: {updated} prices updated from bulk data '
            f'({not_found} cards not found in DB) out of {total}')
        socketio.emit('price_update_progress', dict(_price_update_status))

    except Exception as e:
        _price_update_status['running'] = False
        _price_update_status['message'] = f'Error: {e}'
        socketio.emit('price_update_progress', dict(_price_update_status))
    finally:
        conn.close()


@app.route('/api/collection/prices/update', methods=['POST'])
def api_price_update_start():
    global _price_update_thread
    if _price_update_status['running']:
        return jsonify({'error': 'Price update already running'}), 400
    import threading
    _price_update_thread = threading.Thread(target=_run_price_update, daemon=True)
    _price_update_thread.start()
    return jsonify({'started': True})


@app.route('/api/collection/prices/stop', methods=['POST'])
def api_price_update_stop():
    _price_update_status['running'] = False
    return jsonify({'stopped': True})


@app.route('/api/collection/prices/status')
def api_price_update_status():
    return jsonify(_price_update_status)


# =========================================================================
# E-stop reset
# =========================================================================

@app.route('/api/reset-after-estop', methods=['POST'])
def api_reset_after_estop():
    worker.enqueue('reset_after_estop')
    return jsonify({'queued': True})


# =========================================================================
# Power-loss / unclean-shutdown recovery
# =========================================================================
# Stale sessions are surfaced to the UI via the `stale_session_detected`
# socket event emitted from worker._cmd_check_stale_session at startup.
# This iteration ships detect + discard; Resume (rehydrate tracker)
# lands in Phase 4 with the staged Sort tab rebuild.

@app.route('/api/session/discard-stale', methods=['POST'])
def api_discard_stale_session():
    body = request.get_json(silent=True) or {}
    session_id = body.get('session_id')
    if session_id is None:
        return jsonify({'error': 'missing_session_id'}), 400
    worker.enqueue('discard_stale_session', session_id=int(session_id))
    return jsonify({'queued': True})


@app.route('/api/session/resume-stale', methods=['POST'])
def api_resume_stale_session():
    """Phase 4 part 5 — full power-loss-resume.
    Rebuilds the worker's tracker + sort_config_obj from the session's
    saved metadata and lands in 'paused' so the user clicks Resume."""
    body = request.get_json(silent=True) or {}
    session_id = body.get('session_id')
    if session_id is None:
        return jsonify({'error': 'missing_session_id'}), 400
    worker.enqueue('resume_stale_session', session_id=int(session_id))
    return jsonify({'queued': True})


# =========================================================================
# Source-bin estimated count from probe
# =========================================================================
# Calibration is a one-shot user action: empty the source bin, hit
# this endpoint, the worker probes the bin and saves the Z as the
# reference for future count estimates. The estimate itself is emitted
# automatically via the `source_bin_count_update` socket event whenever
# the source bin is probed during a sort.

@app.route('/api/source-bin/calibrate-empty', methods=['POST'])
def api_calibrate_empty_source_bin():
    worker.enqueue('calibrate_empty_source_bin')
    return jsonify({'queued': True})


@app.route('/api/self-test/run', methods=['POST'])
def api_run_self_test():
    """Trigger the hardware diagnostic sequence. Streams results via
    self_test_step / self_test_complete socket events."""
    worker.enqueue('run_self_test', camera=camera)
    return jsonify({'queued': True})


# =========================================================================
# Support bundle (Phase 3)
# =========================================================================
# Single-button download from Settings → "Download support bundle".
# The first thing we ask for in any support thread.

@app.route('/api/support/bundle', methods=['GET'])
def api_support_bundle():
    """Return a zip with logs, recent scan sessions, runtime state,
    and version info. See support_bundle.py for what's included and
    (more importantly) what's intentionally NOT included."""
    from datetime import datetime as _dt
    import support_bundle
    from config import APP_NAME
    try:
        data = support_bundle.build_support_bundle(
            repo_root=SCRIPT_DIR,
            worker=worker,
            app_name=APP_NAME,
        )
    except Exception as e:
        return jsonify({'error': 'bundle_failed', 'message': str(e)}), 500
    filename = (f"{APP_NAME.lower()}-support-"
                f"{_dt.now().strftime('%Y%m%d_%H%M%S')}.zip")
    return Response(
        data,
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'},
    )


@app.route('/api/source-bin/state', methods=['GET'])
def api_source_bin_state():
    """Return current empty-Z reference + last probed Z + estimate.
    Useful for the UI to populate its source-bin gauge on page load
    without waiting for the next probe-driven event."""
    import gcode_control as _gcode
    empty_z = worker._load_empty_source_z()
    bin_locs = _gcode.get_bin_locations() or {}
    source_x = bin_locs.get(0, _gcode.X_SOURCE_BIN)
    probed_z = _gcode.get_cached_probe_z(source_x)
    estimate = worker._estimate_source_count(probed_z, empty_z) if probed_z is not None else None
    return jsonify({
        'empty_z': empty_z,
        'probed_z': probed_z,
        'estimated_count': estimate,
        'calibrated': empty_z is not None,
        'card_thickness_mm': __import__('config').CARD_THICKNESS_MM,
    })


# =========================================================================
# SocketIO events
# =========================================================================

@socketio.on('connect')
def handle_connect():
    """Send current state when a client connects."""
    socketio.emit('sorter_state', {'state': worker.state})
    socketio.emit('motion_update', motion_tracker.get_state())
    socketio.emit('hardware_status', {
        'connected': worker.state != 'disconnected',
    })
    # Re-check for stale sessions on every browser connect. The worker's
    # startup emission of `stale_session_detected` happens before any
    # browser is connected (so it's lost), and a user reload should
    # re-surface the prompt if they didn't act on it.
    try:
        import collection_db as _cdb
        conn = _cdb.get_connection()
        try:
            stale = _cdb.find_stale_sessions(conn)
        finally:
            conn.close()
        if stale:
            socketio.emit('stale_session_detected', {
                'count': len(stale),
                'primary': stale[0],
                'all': stale,
            })
    except Exception:
        logger.warning("stale-session check on connect failed", exc_info=True)


# =========================================================================
# Startup
# =========================================================================

# =========================================================================
# Foil Labeling Tool
# =========================================================================

FOIL_LABELS_FILE = os.path.join(SCRIPT_DIR, "foil_labels.json")


def _load_foil_labels():
    if os.path.exists(FOIL_LABELS_FILE):
        with open(FOIL_LABELS_FILE, 'r') as f:
            return json.load(f)
    return {}


def _save_foil_labels(labels):
    with open(FOIL_LABELS_FILE, 'w') as f:
        json.dump(labels, f, indent=2)


@app.route('/label')
def label_page():
    return render_template('label.html')


@app.route('/api/label/sessions')
def label_sessions():
    """List sessions that have card crops available."""
    sessions = []
    if not os.path.exists(SCAN_LOGS_DIR):
        return jsonify(sessions)
    for name in sorted(os.listdir(SCAN_LOGS_DIR), reverse=True):
        session_dir = os.path.join(SCAN_LOGS_DIR, name)
        crop_dir = os.path.join(session_dir, "card_crops")
        csv_path = os.path.join(session_dir, "scans.csv")
        if os.path.isdir(crop_dir):
            crop_count = len([f for f in os.listdir(crop_dir)
                              if f.endswith('.jpg')])
            if crop_count > 0:
                sessions.append({
                    'name': name,
                    'crop_count': crop_count,
                    'has_csv': os.path.exists(csv_path),
                })
    return jsonify(sessions)


@app.route('/api/label/cards/<session_name>')
def label_cards(session_name):
    """Get all card crops from a session with scan data and existing labels."""
    import csv

    session_dir = os.path.join(SCAN_LOGS_DIR, session_name)
    crop_dir = os.path.join(session_dir, "card_crops")
    csv_path = os.path.join(session_dir, "scans.csv")

    if not os.path.isdir(crop_dir):
        return jsonify({'error': 'No card crops found'}), 404

    # Load scan CSV for card names
    scan_data = {}
    if os.path.exists(csv_path):
        with open(csv_path, 'r', newline='', encoding='utf-8') as f:
            reader = csv.reader(f)
            header = next(reader, None)
            if header:
                for row in reader:
                    try:
                        scan_num = int(row[0])
                        scan_data[scan_num] = {
                            'name': row[2] if len(row) > 2 else '',
                            'set': row[3] if len(row) > 3 else '',
                            'rarity': row[9] if len(row) > 9 else '',
                            'recognized': row[-1] if row else '',
                        }
                    except (ValueError, IndexError):
                        pass

    # Load existing labels
    labels = _load_foil_labels()

    # Build card list
    cards = []
    for f in sorted(os.listdir(crop_dir)):
        if not f.endswith('.jpg'):
            continue
        # Extract scan number from filename: card_0001.jpg -> 1
        try:
            scan_num = int(f.replace('card_', '').replace('.jpg', ''))
        except ValueError:
            continue

        # Unique key for this card image
        label_key = f"{session_name}/{f}"
        info = scan_data.get(scan_num, {})
        cards.append({
            'file': f,
            'key': label_key,
            'scan_num': scan_num,
            'name': info.get('name', ''),
            'set': info.get('set', ''),
            'rarity': info.get('rarity', ''),
            'label': labels.get(label_key),  # None, "foil", or "nonfoil"
        })

    return jsonify(cards)


@app.route('/api/label/image/<session_name>/<filename>')
def label_image(session_name, filename):
    """Serve a card crop image."""
    session_dir = os.path.join(SCAN_LOGS_DIR, session_name)
    crop_dir = os.path.join(session_dir, "card_crops")
    filepath = os.path.join(crop_dir, filename)
    if not os.path.exists(filepath):
        return "Not found", 404
    return send_file(filepath, mimetype='image/jpeg')


@app.route('/api/label/set', methods=['POST'])
def label_set():
    """Set the foil/nonfoil label for a card image."""
    data = request.get_json()
    key = data.get('key')
    label = data.get('label')  # "foil", "nonfoil", or null to clear
    if not key:
        return jsonify({'error': 'Missing key'}), 400

    labels = _load_foil_labels()
    if label is None:
        labels.pop(key, None)
    else:
        labels[key] = label
    _save_foil_labels(labels)

    return jsonify({'ok': True, 'total': len(labels),
                    'foil': sum(1 for v in labels.values() if v == 'foil'),
                    'nonfoil': sum(1 for v in labels.values() if v == 'nonfoil')})


@app.route('/api/label/stats')
def label_stats():
    """Get labeling stats."""
    labels = _load_foil_labels()
    return jsonify({
        'total': len(labels),
        'foil': sum(1 for v in labels.values() if v == 'foil'),
        'nonfoil': sum(1 for v in labels.values() if v == 'nonfoil'),
    })


def _load_default_bin_config():
    """Load default bin configuration if it exists."""
    default_path = os.path.join(SCRIPT_DIR, 'bin_configs', '_default.json')
    if os.path.exists(default_path):
        try:
            with open(default_path, 'r') as f:
                data = json.load(f)
            import gcode_control

            # Restore machine positions first
            gcode_control.set_machine_positions(
                source_x=data.get('source_x'),
                detection_x=data.get('detection_x'),
                staging_x=data.get('staging_x'),
                staging_width=data.get('staging_width'),
            )

            # If per-bin locations are saved, use those (manual positions)
            if data.get('locations'):
                gcode_control.set_bin_locations(data['locations'])
                count = max((int(k) for k in data['locations'] if int(k) > 0),
                            default=0)
                logger.info(f"Loaded default bin config: {count} bins "
                            f"(per-bin locations)")
            else:
                count = data.get('bin_count', 10)
                start_x = data.get('start_x', 100)
                spacing = data.get('spacing', 100)
                gcode_control.configure_bins(count, start_x=start_x,
                                             spacing=spacing)
                logger.info(f"Loaded default bin config: {count} bins, "
                            f"start={start_x}mm, spacing={spacing}mm")
        except Exception:
            logger.warning("failed to load default bin config",
                           exc_info=True)


# =========================================================================
# Enrichment API (Phase 0A — scheduler status + manual refresh + lookups)
# =========================================================================

@app.route('/api/enrichment/sources', methods=['GET'])
def api_enrichment_sources():
    """List every registered source with its cron, last-run state, and
    coverage summary. Used by the Database subsection in the UI."""
    try:
        entries = enrichment_scheduler.list_sources()
    except Exception as e:
        logging.exception("enrichment sources list failed")
        return jsonify({'error': str(e)}), 500

    coverage = enrichment_repo.coverage_overview()
    for e in entries:
        cov = coverage.get(e['name'])
        if cov:
            e['coverage'] = {
                'last_success': cov.get('last_success'),
                'last_attempt': cov.get('last_attempt'),
                'error': cov.get('error'),
                'coverage_pct': cov.get('coverage_pct'),
            }
        else:
            e['coverage'] = None
    return jsonify({'sources': entries})


@app.route('/api/enrichment/refresh/<source>', methods=['POST'])
def api_enrichment_refresh(source):
    """Manually trigger a refresh. Returns 202 immediately; the actual
    refresh runs on a background thread and emits
    enrichment_refresh_progress + enrichment_refresh_complete events."""
    full = False
    if request.is_json:
        full = bool((request.get_json(silent=True) or {}).get('full', False))
    try:
        enrichment_scheduler.trigger(source, full=full)
    except KeyError:
        return jsonify({'error': f'Unknown source: {source}'}), 404
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 409
    return jsonify({'queued': True, 'source': source}), 202


@app.route('/api/enrichment/probes/run', methods=['POST'])
def api_enrichment_probes_run():
    """Run every probe script synchronously and return the summary.
    Use sparingly — each probe may hit its real endpoint in Phase 0B+.
    """
    try:
        from probes.run_all import run_all
    except Exception as e:
        return jsonify({'error': f'probes import failed: {e}'}), 500
    exit_code, results = run_all()
    return jsonify({
        'ok': exit_code == 0,
        'results': [r.to_dict() for r in results],
    })


# ---------------------------------------------------------------------------
# Sort presets were previously backed by preset_store.py (Phase 0B-1).
# That module and its /api/presets/* endpoints are retired — the unified
# Sort Configuration UI operates directly on sort_configs/*.txt files via
# /api/sort/configs/* and posts inline config_lines on session start.
# ---------------------------------------------------------------------------


@app.route('/api/enrichment/card/<oracle_id>', methods=['GET'])
def api_enrichment_card(oracle_id):
    """Return the full enrichment payload for one oracle_id, or 404."""
    card = enrichment_repo.get_card(oracle_id)
    if card is None:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'oracle_id': card.oracle_id,
        'tags': card.tags,
        'staples': card.staples,
        'salt': card.salt,
        'combos': card.combos,
        'buylists': card.buylists,
        'themes': card.themes,
        'commander_rank': card.commander_rank,
        'source_freshness': card.source_freshness,
    })


# =========================================================================
# Moxfield integrations (Phase 3 item 3.15)
# =========================================================================

_moxfield_source = None


def _get_moxfield_source():
    """Lazy-init MoxfieldSource; avoids import cost at server start."""
    global _moxfield_source
    if _moxfield_source is None:
        from web_enrichment.moxfield import MoxfieldSource
        _moxfield_source = MoxfieldSource()
    return _moxfield_source


@app.route('/api/integrations/moxfield/deck/import', methods=['POST'])
def api_moxfield_deck_import():
    """Fetch a public Moxfield deck by URL and cache it.

    Body (JSON):
      {
        "deck_url":          "<full Moxfield URL or bare deck ID>",
        "printing_mode":     "any" | "exact"   (default "any"),
        "include_sideboard": false,
        "use_cache":         true
      }

    Basic lands are excluded from the returned card list regardless of
    printing_mode.

    Returns 200 with:
      { deck_id, deck_name, format, owner, printing_mode, card_count,
        warnings, cards: [{oracle_id, name, quantity, set,
                            collector_number, scryfall_id, board,
                            printing_mode}] }

    Returns 400 if deck_url is missing/unparseable or printing_mode invalid.
    Returns 404 if Moxfield returns 404 (deck not found / private).
    Returns 503 if the network request fails (Moxfield unreachable).
    """
    import httpx as _httpx
    from web_enrichment.moxfield import MoxfieldSource, VALID_PRINTING_MODES

    body = request.get_json(silent=True) or {}
    deck_url = body.get('deck_url') or ''
    if not deck_url:
        return jsonify({'error': 'deck_url is required'}), 400

    include_side = bool(body.get('include_sideboard', False))
    use_cache = bool(body.get('use_cache', True))
    printing_mode = str(body.get('printing_mode') or 'any').lower()
    if printing_mode not in VALID_PRINTING_MODES:
        return jsonify({
            'error': f'Invalid printing_mode {printing_mode!r}; '
                     f'must be one of: {list(VALID_PRINTING_MODES)}'
        }), 400

    try:
        result = _get_moxfield_source().import_deck(
            deck_url,
            include_side=include_side,
            use_cache=use_cache,
            printing_mode=printing_mode,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except _httpx.HTTPStatusError as e:
        code = e.response.status_code if e.response is not None else 0
        if code == 404:
            return jsonify({'error': 'Deck not found or is private on Moxfield'}), 404
        return jsonify({'error': f'Moxfield returned HTTP {code}'}), 502
    except _httpx.RequestError as e:
        return jsonify({'error': f'Could not reach Moxfield: {type(e).__name__}'}), 503
    except Exception as e:
        logging.error('[moxfield] import error: %s', e, exc_info=True)
        return jsonify({'error': f'Import failed: {type(e).__name__}: {e}'}), 500

    return jsonify(result)


@app.route('/api/integrations/moxfield/deck/<deck_id>/cache', methods=['GET'])
def api_moxfield_deck_cache(deck_id):
    """Return the cached import payload for a deck, or 404 if not cached.

    Query params: ?printing_mode=any|exact  (default "any")

    Does NOT hit the Moxfield API — returns only what is already stored in
    enrichment.db.  Use /import to fetch-and-cache a new deck.
    """
    from web_enrichment.moxfield import get_cached_deck, VALID_PRINTING_MODES
    printing_mode = (request.args.get('printing_mode') or 'any').lower()
    if printing_mode not in VALID_PRINTING_MODES:
        return jsonify({
            'error': f'Invalid printing_mode; must be one of {list(VALID_PRINTING_MODES)}'
        }), 400
    result = get_cached_deck(deck_id, printing_mode=printing_mode)
    if result is None:
        return jsonify({'error': 'Deck not in cache; use /import to fetch it'}), 404
    return jsonify(result)


@app.route('/api/integrations/moxfield/wishlist/import', methods=['POST'])
def api_moxfield_wishlist_import():
    """Fetch a user's public Moxfield wishlist and cache it.

    Body (JSON):
      {
        "username":      "<Moxfield username>",
        "printing_mode": "any" | "exact"   (default "any"),
        "use_cache":     true
      }

    Basic lands are excluded from the returned card list regardless of
    printing_mode.

    Authentication: public wishlists only (no OAuth in v1).  Users whose
    wishlist is set to private will receive a 401 from Moxfield, surfaced
    here as 401.

    Wishlist API endpoint used:
      GET https://api2.moxfield.com/v2/users/<username>/wishlist

    Returns 200 with:
      { username, printing_mode, card_count, warnings,
        cards: [{oracle_id, name, quantity, set, collector_number,
                 scryfall_id, printing_mode}] }

    Returns 400 if username is missing or printing_mode invalid.
    Returns 401 if the wishlist is private (Moxfield returns 401).
    Returns 404 if the user or wishlist does not exist on Moxfield.
    Returns 503 if the network request fails.
    """
    import httpx as _httpx
    from web_enrichment.moxfield import MoxfieldSource, VALID_PRINTING_MODES

    body = request.get_json(silent=True) or {}
    username = (body.get('username') or '').strip()
    if not username:
        return jsonify({'error': 'username is required'}), 400

    printing_mode = str(body.get('printing_mode') or 'any').lower()
    if printing_mode not in VALID_PRINTING_MODES:
        return jsonify({
            'error': f'Invalid printing_mode {printing_mode!r}; '
                     f'must be one of: {list(VALID_PRINTING_MODES)}'
        }), 400

    use_cache = bool(body.get('use_cache', True))

    try:
        result = _get_moxfield_source().import_wishlist(
            username,
            use_cache=use_cache,
            printing_mode=printing_mode,
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except _httpx.HTTPStatusError as e:
        code = e.response.status_code if e.response is not None else 0
        if code == 401:
            return jsonify({'error': 'Wishlist is private; authentication required'}), 401
        if code == 404:
            return jsonify({'error': 'User or wishlist not found on Moxfield'}), 404
        return jsonify({'error': f'Moxfield returned HTTP {code}'}), 502
    except _httpx.RequestError as e:
        return jsonify({'error': f'Could not reach Moxfield: {type(e).__name__}'}), 503
    except Exception as e:
        logging.error('[moxfield] wishlist import error: %s', e, exc_info=True)
        return jsonify({'error': f'Import failed: {type(e).__name__}: {e}'}), 500

    return jsonify(result)


_shutting_down = False


def _graceful_shutdown(reason='shutdown'):
    """
    Single shutdown path used by atexit + signal handlers.
    Stops the camera, kills pumps, ends any active session, stops the
    worker thread, and disconnects from serial. Idempotent.
    """
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logger.info(f"Graceful shutdown initiated ({reason})")

    # 1. Trigger an emergency stop if we're mid-motion so pumps/motors
    #    drop immediately even if the worker is stuck in a command.
    try:
        import gcode_control
        if gcode_control.is_connected() and gcode_control.ser:
            try:
                gcode_control.ser.write(b'M106 P0 S0\n')
                gcode_control.ser.write(b'M106 P1 S0\n')
                logger.info("Pumps killed")
            except Exception as e:
                logger.warning(f"pump-off failed: {e}")
    except Exception:
        pass

    # 2. End any active sort session so the DB gets closed cleanly.
    try:
        if getattr(worker, 'tracker', None) is not None:
            try:
                worker.tracker.end_session()
                logger.info("Active session ended")
            except Exception as e:
                logger.warning(f"session end failed: {e}")
            worker.tracker = None
    except Exception:
        pass

    # 3. Stop the camera thread.
    try:
        if camera.is_active:
            camera.stop()
            logger.info("Camera stopped")
    except Exception as e:
        logger.warning(f"camera.stop failed: {e}")

    # 4. Stop the worker thread (drains queue, joins).
    try:
        worker.stop()
        logger.info("Worker stopped")
    except Exception as e:
        logger.warning(f"worker.stop failed: {e}")

    # 5. Disconnect serial and flush gcode trace log.
    try:
        import gcode_control
        if gcode_control.is_connected():
            gcode_control.close_connection()
            logger.info("Serial disconnected")
        gcode_control.close_gcode_log()
    except Exception as e:
        logger.warning(f"disconnect failed: {e}")

    # 6. Stop the enrichment scheduler.
    try:
        enrichment_scheduler.shutdown()
        logger.info("Enrichment scheduler stopped")
    except Exception as e:
        logger.warning(f"enrichment scheduler shutdown failed: {e}")

    logger.info("Shutdown complete")


def _signal_handler(signum, frame):
    logger.info(f"Received signal {signum}")
    _graceful_shutdown(reason=f'signal {signum}')
    sys.exit(0)


def _run_health_check():
    """
    Startup sanity check — surface problems early instead of waiting
    until the user hits 'Connect' or 'Start Session' and gets a
    confusing error. Non-fatal: prints warnings but doesn't block
    startup.
    """
    print()
    print("-" * 60)
    print("  Startup health check")
    print("-" * 60)
    warnings = []

    # Cards JSON
    try:
        import config as _cfg
        if not os.path.exists(_cfg.CARDS_JSON_PATH):
            warnings.append(f"CARDS_JSON_PATH missing: {_cfg.CARDS_JSON_PATH}")
        else:
            size_mb = os.path.getsize(_cfg.CARDS_JSON_PATH) / (1024 * 1024)
            print(f"  [OK] Cards JSON: {os.path.basename(_cfg.CARDS_JSON_PATH)} "
                  f"({size_mb:.1f} MB)")
    except Exception as e:
        warnings.append(f"config import failed: {e}")

    # Hash DBs
    try:
        import config as _cfg
        for name, path in (('v1 hash DB', _cfg.HASH_DB_PATH),
                           ('v2 hash DB', _cfg.HASH_DB_V2_PATH)):
            if os.path.exists(path):
                size_mb = os.path.getsize(path) / (1024 * 1024)
                print(f"  [OK] {name}: {os.path.basename(path)} "
                      f"({size_mb:.1f} MB)")
            else:
                warnings.append(f"{name} missing: {path}")
    except Exception as e:
        warnings.append(f"hash DB check failed: {e}")

    # Collection DB
    try:
        import config as _cfg
        if hasattr(_cfg, 'COLLECTION_DB_PATH'):
            if os.path.exists(_cfg.COLLECTION_DB_PATH):
                print(f"  [OK] Collection DB: "
                      f"{os.path.basename(_cfg.COLLECTION_DB_PATH)}")
            else:
                print(f"  [info] Collection DB will be created on first use")
    except Exception:
        pass

    # Scan logs dir
    try:
        os.makedirs(SCAN_LOGS_DIR, exist_ok=True)
        print(f"  [OK] Scan logs dir: {SCAN_LOGS_DIR}")
    except Exception as e:
        warnings.append(f"cannot create scan logs dir: {e}")

    # Sort configs dir
    try:
        os.makedirs(SORT_CONFIGS_DIR, exist_ok=True)
        print(f"  [OK] Sort configs dir: {SORT_CONFIGS_DIR}")
    except Exception as e:
        warnings.append(f"cannot create sort configs dir: {e}")

    # Serial ports available
    try:
        import serial.tools.list_ports
        ports = list(serial.tools.list_ports.comports())
        if ports:
            print(f"  [OK] Serial ports found: "
                  f"{', '.join(p.device for p in ports)}")
        else:
            print(f"  [warn] No serial ports detected "
                  f"(ok if hardware not connected)")
    except Exception as e:
        warnings.append(f"serial port probe failed: {e}")

    # Camera probe (non-blocking — just verify OpenCV imports)
    try:
        import cv2  # noqa: F401
        print(f"  [OK] OpenCV available: {cv2.__version__}")
    except Exception as e:
        warnings.append(f"OpenCV import failed: {e}")

    if warnings:
        print()
        print("  WARNINGS:")
        for w in warnings:
            print(f"    ! {w}")
    else:
        print("  All checks passed.")
    print("-" * 60)
    print()
    return warnings


def _preload_heavy_modules():
    """
    Eagerly import the modules whose module-level load phases are slow
    (tens of seconds on cold disk). Doing this at startup trades a bit
    of boot time for a huge win: the first real Detect press no longer
    blocks the worker thread on a ~50MB JSON parse + hash DB load,
    which used to stall the camera capture thread long enough for the
    watchdog to trip and auto-pause the session before any motion
    happened. These modules are used inside _cmd_detect_and_sort and
    were previously imported lazily there.

    Safe to call multiple times — Python caches imported modules.
    """
    import time as _t
    print()
    print("-" * 60)
    print("  Preloading hot-path modules (cards DB, hash DB, detection)")
    print("-" * 60)
    t0 = _t.time()
    try:
        # cards.py parses CARDS_JSON (~50MB) + PRINTINGS_MAP at import.
        import cards  # noqa: F401
        # card_identify_hybrid.py loads phash DB + DINOv2 model at import.
        import card_identify_hybrid  # noqa: F401
        # card_detect.py: OpenCV-based contour detection.
        import card_detect  # noqa: F401
        # sorting.py: sort config, bin rules.
        import sorting  # noqa: F401
        # collection_db for wishlist / history queries.
        import collection_db  # noqa: F401
        # Scryfall card info extraction.
        try:
            from cards import CARD_DATA_BY_ID
            card_count = len(CARD_DATA_BY_ID)
        except Exception:
            card_count = 0
        print(f"  [OK] Hot-path modules preloaded "
              f"({card_count} cards) in {_t.time() - t0:.1f}s")
    except Exception as e:
        print(f"  [warn] Preload failed: {e} — first detect will be slow")
    print("-" * 60)
    print()


def main():
    print("=" * 60)
    print("  MTG Card Sorter — Web Server")
    print("=" * 60)
    print()
    print("  Open http://localhost:5000 in your browser")
    print("  Press Ctrl+C to stop the server")
    print()

    # Startup health check — warn about missing files / serial ports etc.
    _run_health_check()

    # Preload heavy modules (cards JSON, hash DBs, detection). Without
    # this, the first Detect press inside a sort session blocks the
    # worker thread for 20+ seconds on module-level JSON parsing and
    # triggers the camera watchdog, which auto-pauses the session
    # before any card can be sorted.
    _preload_heavy_modules()

    # Install shutdown handlers before anything else that could fail,
    # so Ctrl+C still cleans up during startup.
    atexit.register(_graceful_shutdown, 'atexit')
    try:
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
    except (ValueError, AttributeError) as e:
        # Windows / non-main-thread limitations
        logger.warning(f"signal handler install partial: {e}")

    # Camera health listener — auto-pause the sort if the camera
    # ACTUALLY stays dead mid-session. We can't recognize cards
    # without a live feed, so continuing would drop cards into the
    # wrong bins.
    #
    # Important nuance: the camera's watchdog can briefly flip health
    # to 'stalled' or 'dead' whenever it triggers a reconnect, even
    # though it recovers within 1-2 seconds on attempt 1. The old
    # handler auto-paused on every such flip, which killed the sort
    # session the first time anything (heavy JSON parse, CPU spike)
    # momentarily starved the capture thread. Instead we use a grace
    # window: only auto-pause after health has been continuously
    # non-ok for GRACE_SECONDS and the camera is still not healthy.
    import threading as _cam_thr_mod

    GRACE_SECONDS = 12.0
    _health_pause_lock = _cam_thr_mod.Lock()
    _health_pause_state = {'timer': None, 'unhealthy_since': None}

    def _cancel_pause_timer():
        t = _health_pause_state.get('timer')
        if t is not None:
            try:
                t.cancel()
            except Exception:
                pass
            _health_pause_state['timer'] = None
        _health_pause_state['unhealthy_since'] = None

    def _do_auto_pause(reason):
        # Re-check state at fire time so a late-arriving recovery wins.
        with _health_pause_lock:
            if camera.health == 'ok':
                logger.info(f"Camera recovered before grace expired "
                            f"— not pausing")
                _cancel_pause_timer()
                return
            if worker.state != 'sorting':
                _cancel_pause_timer()
                return
            logger.warning(f"Camera still {camera.health} after "
                           f"{GRACE_SECONDS:.0f}s grace — auto-pausing sort")
            try:
                worker.request_abort('camera freeze')
                worker.enqueue('pause')
                socketio.emit('error', {
                    'message': (f'Camera {camera.health} for '
                                f'{GRACE_SECONDS:.0f}s during sort — '
                                f'session auto-paused. Check camera '
                                f'and resume.'),
                })
            except Exception:
                logger.exception("auto-pause failed")
            _cancel_pause_timer()

    def _on_camera_health(old, new):
        try:
            socketio.emit('camera_health', {
                'old': old,
                'new': new,
            })
        except Exception:
            pass

        with _health_pause_lock:
            if new == 'ok':
                # Healthy again — cancel any pending auto-pause.
                if _health_pause_state['timer'] is not None:
                    logger.info(f"Camera recovered ({old}->ok) "
                                f"— cancelling auto-pause timer")
                _cancel_pause_timer()
                return

            # Non-ok transition. Only meaningful during an active sort.
            if worker.state != 'sorting':
                return

            # Already have a timer running from an earlier transition?
            # Leave it alone — it started the grace countdown already.
            if _health_pause_state['timer'] is not None:
                return

            _health_pause_state['unhealthy_since'] = time.time()
            timer = _cam_thr_mod.Timer(
                GRACE_SECONDS,
                _do_auto_pause,
                args=(f'{old}->{new}',),
            )
            timer.daemon = True
            _health_pause_state['timer'] = timer
            timer.start()
            logger.warning(f"Camera health {old}->{new} during sort — "
                           f"starting {GRACE_SECONDS:.0f}s grace window before "
                           f"auto-pause")
    try:
        camera.add_health_listener(_on_camera_health)
    except Exception as e:
        logger.warning(f"could not install camera health listener: {e}")

    # Wire serial error callback so serial dropouts auto-notify the UI.
    try:
        import gcode_control
        def _on_serial_error(err):
            try:
                socketio.emit('hardware_status', {
                    'connected': False,
                    'error': str(err),
                })
                socketio.emit('error', {'message': f'Serial error: {err}'})
            except Exception:
                pass
            # Force worker state back to disconnected.
            try:
                if worker.state != 'disconnected':
                    worker.state = 'disconnected'
            except Exception:
                pass
        gcode_control.set_serial_error_callback(_on_serial_error)
    except Exception as e:
        logger.warning(f"could not install serial error callback: {e}")

    # Load default bin config before starting
    _load_default_bin_config()

    # Restore the last calibration result, if one was auto-saved. This
    # makes server restarts transparent — no need to re-run calibration
    # every time. If the file doesn't exist (first run) or is corrupt,
    # this is a silent no-op.
    try:
        restored = worker.load_last_setup()
        if restored:
            logger.info(f"Restored last setup from disk: "
                        f"{restored.get('dest_bin_count')} dest bins, "
                        f"{restored.get('source_bin_count')} source bins")
    except Exception as e:
        logger.warning(f"load_last_setup failed: {e}")

    # Start the worker thread
    worker.start()

    # Auto-connect to the hardware on boot. Failure is non-fatal —
    # gcode_control.connect_to_board() handles missing/busy serial
    # ports gracefully, and the UI shows Disconnected so the user can
    # retry from Setup. This is the precondition for the autonomy
    # ladder's "auto-home with confirm on first connect" prompt
    # (plans/autonomy_ladder.md). Opt out by setting
    # CARD_SORTER_NO_AUTO_CONNECT=1 in the environment.
    if os.environ.get('CARD_SORTER_NO_AUTO_CONNECT', '').strip() in ('', '0', 'false', 'False'):
        logger.info("Auto-connecting to hardware (set CARD_SORTER_NO_AUTO_CONNECT=1 to disable)")
        worker.enqueue('connect')
    else:
        logger.info("Auto-connect disabled via CARD_SORTER_NO_AUTO_CONNECT")

    # Check for sessions that didn't end cleanly (power loss, crash).
    # If found, the worker emits `stale_session_detected` and the UI
    # banner offers a Discard action. See plans/sort_flow_stages.md
    # "Power-loss resume" — full Resume rehydration is Phase 4 work;
    # this lands the detect + discard half so stale rows stop piling up.
    worker.enqueue('check_stale_session')

    # Start the enrichment refresh scheduler. Failure here is non-fatal
    # — the core sorting flow does not depend on enrichment.
    try:
        enrichment_scheduler.start()
        logger.info("Enrichment scheduler started")
    except Exception as e:
        logger.warning(f"enrichment scheduler start failed: {e}")

    # Run Flask with SocketIO
    socketio.run(app, host='0.0.0.0', port=5000,
                 debug=False, allow_unsafe_werkzeug=True)


if __name__ == '__main__':
    main()
