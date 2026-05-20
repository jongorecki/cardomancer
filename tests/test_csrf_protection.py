"""
CSRF protection tests for the Flask before_request hook.

The hook in web_server.py rejects state-changing requests (POST/PUT/
DELETE/PATCH) whose Origin and Referer headers don't match the kiosk's
host, UNLESS the request carries `X-Requested-With: XMLHttpRequest`
(which cross-origin browsers cannot set without a CORS preflight we
never serve).

Bypasses:
  - Safe methods (GET/HEAD/OPTIONS) always pass through.
  - app.config['TESTING'] flips the whole check off (set by the rest
    of the test suite's autouse fixture in conftest.py).
  - Paths under /socket.io/ are exempt (their own auth gates).

These tests explicitly disable the TESTING bypass so the real hook
runs end-to-end. They use a route that exists in the live app.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import web_server


# A POST route we can hit safely without hardware or DB side effects.
# /api/sort/validate-query is a pure parser round-trip — it doesn't
# touch the worker or any persistent state. Any 200 body confirms
# the request was accepted past the CSRF gate; any 403 with
# error=csrf_failed confirms it was rejected.
_TEST_POST_PATH = '/api/sort/validate-query'
_TEST_POST_BODY = {'query': 'c:r'}


class CSRFCheckTests(unittest.TestCase):
    """Drive the CSRF middleware directly. Toggles TESTING off so the
    real Origin/Referer/X-Requested-With logic runs."""

    @classmethod
    def setUpClass(cls):
        cls.app = web_server.app

    def setUp(self):
        # The autouse fixture in conftest.py sets TESTING=True; flip
        # it off so we exercise the production path. The fixture's
        # `finally` will restore it after the test runs.
        self.app.config['TESTING'] = False
        self.client = self.app.test_client()

    # ------------------------------------------------------------------
    # Accept paths
    # ------------------------------------------------------------------

    def test_get_request_passes_without_headers(self):
        """Safe methods skip the CSRF check entirely."""
        r = self.client.get('/api/status')
        self.assertNotEqual(r.status_code, 403,
                            "GET /api/status should not be CSRF-gated")

    def test_post_with_xrequested_with_header_passes(self):
        """X-Requested-With: XMLHttpRequest is the fast-path bypass."""
        r = self.client.post(
            _TEST_POST_PATH,
            json=_TEST_POST_BODY,
            headers={'X-Requested-With': 'XMLHttpRequest'},
        )
        self.assertNotEqual(r.status_code, 403,
                            f"POST with X-Requested-With should pass; "
                            f"got {r.status_code} {r.get_data(as_text=True)!r}")

    def test_post_with_matching_origin_passes(self):
        """A POST whose Origin matches request.host_url is allowed
        even without X-Requested-With."""
        r = self.client.post(
            _TEST_POST_PATH,
            json=_TEST_POST_BODY,
            headers={'Origin': 'http://localhost'},
        )
        self.assertNotEqual(r.status_code, 403,
                            "Same-origin POST should pass")

    def test_post_with_matching_referer_passes(self):
        """A POST whose Referer starts with request.host_url is
        allowed even when Origin is absent."""
        r = self.client.post(
            _TEST_POST_PATH,
            json=_TEST_POST_BODY,
            headers={'Referer': 'http://localhost/'},
        )
        self.assertNotEqual(r.status_code, 403,
                            "Same-origin Referer POST should pass")

    # ------------------------------------------------------------------
    # Reject paths
    # ------------------------------------------------------------------

    def test_post_with_no_headers_is_rejected(self):
        """A bare cross-origin-style POST (no Origin, no Referer,
        no X-Requested-With) is the canonical CSRF attack — block."""
        r = self.client.post(_TEST_POST_PATH, json=_TEST_POST_BODY)
        self.assertEqual(r.status_code, 403)
        body = r.get_json()
        self.assertEqual(body.get('error'), 'csrf_failed')

    def test_post_with_foreign_origin_is_rejected(self):
        """An Origin header from a different host is the textbook
        cross-site POST. Block."""
        r = self.client.post(
            _TEST_POST_PATH,
            json=_TEST_POST_BODY,
            headers={'Origin': 'http://evil.example.com'},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json().get('error'), 'csrf_failed')

    def test_post_with_foreign_referer_is_rejected(self):
        """Spoof-defence: Referer from a different host is rejected
        even when X-Requested-With is absent."""
        r = self.client.post(
            _TEST_POST_PATH,
            json=_TEST_POST_BODY,
            headers={'Referer': 'http://evil.example.com/some-page'},
        )
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.get_json().get('error'), 'csrf_failed')

    def test_post_with_origin_prefix_collision_is_rejected(self):
        """`http://localhost.attacker.com` starts with the same
        characters as `http://localhost` but is a different host.
        The matcher must not be confused by string-prefix."""
        r = self.client.post(
            _TEST_POST_PATH,
            json=_TEST_POST_BODY,
            headers={'Origin': 'http://localhost.attacker.com'},
        )
        self.assertEqual(r.status_code, 403,
                         "Prefix collision must not bypass CSRF check")

    # ------------------------------------------------------------------
    # Exempt paths
    # ------------------------------------------------------------------

    def test_socketio_path_is_exempt(self):
        """Socket.IO endpoints have their own connection-level gating
        and must not be filtered through CSRF — otherwise the
        long-poll fallback breaks."""
        # We don't actually hit a real Socket.IO endpoint (would need
        # a live server). Instead, verify the path matcher allows
        # /socket.io/ prefixes through the CSRF function directly.
        from web_server import _is_csrf_exempt
        self.assertTrue(_is_csrf_exempt('/socket.io/'))
        self.assertTrue(_is_csrf_exempt('/socket.io/?EIO=4'))
        self.assertFalse(_is_csrf_exempt('/api/sort/start'))
        self.assertFalse(_is_csrf_exempt('/'))

    # ------------------------------------------------------------------
    # Bypass switch
    # ------------------------------------------------------------------

    def test_testing_flag_bypasses_check(self):
        """app.config['TESTING'] = True turns the whole gate off so
        the rest of the suite isn't forced to carry CSRF headers in
        every test fixture."""
        self.app.config['TESTING'] = True
        r = self.client.post(_TEST_POST_PATH, json=_TEST_POST_BODY)
        # The exact non-403 status code depends on the body; what
        # matters is that the CSRF check didn't fire.
        self.assertNotEqual(r.status_code, 403,
                            "TESTING flag must bypass CSRF gate")


class SecretKeyTests(unittest.TestCase):
    """Cardomancer-specific SECRET_KEY loader."""

    def test_env_var_overrides_disk_key(self):
        """If $CARDOMANCER_SECRET_KEY is set, the loader returns it
        and never touches the home-dir file."""
        import importlib
        os.environ['CARDOMANCER_SECRET_KEY'] = 'sentinel-env-key-12345'
        try:
            # _load_or_create_secret_key is module-private; access
            # through the module for direct invocation.
            key = web_server._load_or_create_secret_key()
            self.assertEqual(key, 'sentinel-env-key-12345')
        finally:
            del os.environ['CARDOMANCER_SECRET_KEY']

    def test_persisted_key_is_secret_strength(self):
        """The on-disk key is at least 32 bytes of hex (64 chars).
        Belt-and-suspenders: confirms _load_or_create_secret_key
        isn't accidentally returning a short fallback."""
        # Force the disk path through a tempdir so we don't touch
        # the user's actual key.
        import tempfile
        old_home = os.environ.get('HOME')
        old_userprofile = os.environ.get('USERPROFILE')
        tmpdir = tempfile.mkdtemp(prefix='cm_secret_test_')
        try:
            os.environ['HOME'] = tmpdir
            os.environ['USERPROFILE'] = tmpdir
            # Clear the env-var fast path too
            os.environ.pop('CARDOMANCER_SECRET_KEY', None)
            key = web_server._load_or_create_secret_key()
            self.assertGreaterEqual(len(key), 32,
                                    "SECRET_KEY must be at least 32 chars")
            # All hex (the secrets.token_hex(32) result)
            int(key, 16)  # raises ValueError if not hex
        finally:
            if old_home is not None:
                os.environ['HOME'] = old_home
            else:
                os.environ.pop('HOME', None)
            if old_userprofile is not None:
                os.environ['USERPROFILE'] = old_userprofile
            else:
                os.environ.pop('USERPROFILE', None)
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
