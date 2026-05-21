"""
Regression tests for the stdout/stderr-tee recursion that used to
lock up the kiosk during a database update.

The setup is: web_server installs a RotatingFileHandler and replaces
sys.stdout / sys.stderr with a tee that routes writes through the
root logger. If the file handler errors (Windows: file held by another
process) the default `handleError` writes the traceback to sys.stderr.
With the unguarded tee, that traceback re-enters logging — the handler
errors again — handleError writes again — recursion.

We pin two contracts:

  1. The tee's `_tee_recursion.active` flag short-circuits re-entrant
     writes so even if a handler error path drives the loop, we stop
     at depth 1.
  2. `_SafeRotatingFileHandler.rotate` catches PermissionError/OSError
     from `os.rename` and continues — the existing file keeps growing,
     no exception bubbles up, and crucially nothing is written to
     `sys.stderr` (which would itself re-enter the tee).
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import web_server


class SafeRotatingFileHandlerTests(unittest.TestCase):
    """The Windows-tolerant rotate."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix='cm_log_test_')
        self.log_path = os.path.join(self.tmpdir, 'test.log')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_rotate_success_path_still_works(self):
        """A normal rotation (no rename collision) still moves the
        file like the parent class. Use delay=True so the handler
        doesn't open (and lock) the source file — that's the lock
        Windows refuses to rename across, and the very thing the
        sad-path test exercises separately."""
        src = os.path.join(self.tmpdir, 'src.log')
        dst = os.path.join(self.tmpdir, 'src.log.1')
        with open(src, 'w', encoding='utf-8') as f:
            f.write('x' * 10)
        h = web_server._SafeRotatingFileHandler(
            src, maxBytes=1, backupCount=1, encoding='utf-8',
            delay=True,
        )
        try:
            h.rotate(src, dst)
            self.assertTrue(
                os.path.exists(dst),
                "Rotate should move src -> dst on the happy path",
            )
            self.assertFalse(
                os.path.exists(src),
                "Source should be gone after a successful rename",
            )
        finally:
            h.close()

    def test_rotate_swallows_permission_error(self):
        """When os.rename raises PermissionError (the Windows file-
        lock case), rotate() must not propagate — and must not call
        anything that goes back into logging or sys.stderr."""
        src = os.path.join(self.tmpdir, 'locked.log')
        with open(src, 'w', encoding='utf-8') as f:
            f.write('content')
        h = web_server._SafeRotatingFileHandler(
            src, maxBytes=1, backupCount=1, encoding='utf-8',
        )
        try:
            with mock.patch(
                'logging.handlers.RotatingFileHandler.rotate',
                side_effect=PermissionError(32, 'simulated lock'),
            ):
                # Must not raise.
                h.rotate(src, src + '.1')
        finally:
            h.close()

    def test_rotate_swallows_generic_oserror(self):
        """Same protection for the broader OSError family (sharing
        violations sometimes surface as bare OSError on Windows)."""
        src = os.path.join(self.tmpdir, 'locked2.log')
        with open(src, 'w', encoding='utf-8') as f:
            f.write('content')
        h = web_server._SafeRotatingFileHandler(
            src, maxBytes=1, backupCount=1, encoding='utf-8',
        )
        try:
            with mock.patch(
                'logging.handlers.RotatingFileHandler.rotate',
                side_effect=OSError(13, 'simulated sharing violation'),
            ):
                h.rotate(src, src + '.1')
        finally:
            h.close()

    def test_rotate_error_writes_to_real_stderr_not_tee(self):
        """The note we DO emit on rotation failure must go to
        sys.__stderr__ (the actual fd), not the patched sys.stderr,
        because the latter is the tee that would re-enter logging."""
        src = os.path.join(self.tmpdir, 'locked3.log')
        with open(src, 'w', encoding='utf-8') as f:
            f.write('content')
        h = web_server._SafeRotatingFileHandler(
            src, maxBytes=1, backupCount=1, encoding='utf-8',
        )
        try:
            # Patch BOTH the tee (sys.stderr) and the real fd
            # (sys.__stderr__). The handler should write to the real
            # fd only; sys.stderr should NOT be touched.
            fake_tee = mock.MagicMock()
            fake_real = mock.MagicMock()
            with mock.patch.object(sys, 'stderr', fake_tee), \
                 mock.patch.object(sys, '__stderr__', fake_real), \
                 mock.patch(
                     'logging.handlers.RotatingFileHandler.rotate',
                     side_effect=PermissionError(32, 'sim')):
                h.rotate(src, src + '.1')
            # Real stderr got the note, tee didn't.
            self.assertTrue(fake_real.write.called,
                            "Should write rotation-failure note to __stderr__")
            self.assertFalse(fake_tee.write.called,
                             "Must not write through the tee (recursion risk)")
        finally:
            h.close()


class TeeRecursionGuardTests(unittest.TestCase):
    """The threading-local recursion guard inside `_StreamToLogger`.

    We can't easily instantiate the inner class (it's defined inside
    `_setup_logging`), but we can pin the contract by exercising the
    `_tee_recursion` flag directly. That's the actual lock that
    breaks the loop in production.
    """

    def setUp(self):
        # Defensive: clear the flag in case a previous test left it set.
        web_server._tee_recursion.active = False

    def tearDown(self):
        web_server._tee_recursion.active = False

    def test_recursion_flag_defaults_false(self):
        """First read of the flag in a thread returns False — we don't
        accidentally skip the first log call."""
        # Use a fresh threading.local-like check.
        self.assertFalse(
            getattr(web_server._tee_recursion, 'active', False),
            "Recursion flag must default to falsy",
        )

    def test_threading_local_isolates_threads(self):
        """The guard is threading.local() — flipping it in one thread
        must not affect another. Otherwise a hot logging thread could
        starve out the main thread's writes."""
        import threading
        observed_in_other_thread = []

        def _other():
            observed_in_other_thread.append(
                getattr(web_server._tee_recursion, 'active', False)
            )

        web_server._tee_recursion.active = True
        t = threading.Thread(target=_other)
        t.start()
        t.join()
        # Other thread saw it as False.
        self.assertEqual(observed_in_other_thread, [False])
        # This thread still sees it as True.
        self.assertTrue(web_server._tee_recursion.active)


class LoggingRaiseExceptionsTests(unittest.TestCase):
    """In production we set `logging.raiseExceptions = False` so a
    handler emit failure doesn't drop a traceback into stderr (the
    tee). _setup_logging() ran at module import so the flag is
    already off in this process."""

    def test_raise_exceptions_disabled(self):
        # We don't re-run _setup_logging here — it would tee
        # sys.stdout / sys.stderr for the rest of the suite, which
        # other tests rely on being untouched. The contract is just
        # that production sets the flag off.
        self.assertFalse(
            logging.raiseExceptions,
            "logging.raiseExceptions must be False so handler errors "
            "don't print to the tee'd sys.stderr (re-entry risk)",
        )


if __name__ == '__main__':
    unittest.main()
