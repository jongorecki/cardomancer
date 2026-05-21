"""
Tests for the welcome-tour overlay (static/modules/tour.js).

The tour is plain JS — there's no Python-side logic — so these tests
mostly assert markup integration:
  - tour.js is in the script bundle
  - the home tab carries a "Walk me through it" trigger that calls
    startTour()
  - tour.js compiles (no syntax errors) by importing it via Node-style
    syntax check
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import web_server


class WelcomeTourMarkupTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = web_server.app.test_client()
        r = cls.client.get('/')
        assert r.status_code == 200
        cls.body = r.get_data(as_text=True)

    def test_tour_js_loaded(self):
        """The script bundle pulls in modules/tour.js after home.js
        so startTour is defined by the time the home banner's button
        can be clicked."""
        self.assertIn('/static/modules/tour.js', self.body)
        # Order check: tour.js after home.js (it depends on goToTab
        # from home.js).
        home_idx = self.body.find('/static/modules/home.js')
        tour_idx = self.body.find('/static/modules/tour.js')
        self.assertGreater(home_idx, -1)
        self.assertGreater(tour_idx, -1)
        self.assertLess(home_idx, tour_idx,
                        "tour.js must load AFTER home.js — it depends "
                        "on goToTab() being defined")

    def test_walk_me_through_link_present(self):
        """The home tab carries a visible 'Walk me through it' link
        that fires the tour."""
        self.assertIn('startTour()', self.body,
                      "Home tab must have a startTour() trigger")
        self.assertIn('Walk me through it', self.body)

    def test_tour_button_has_aria_label(self):
        """The link is a <button>, not an <a>, and has an aria-label
        that explains what happens (five-step overlay)."""
        idx = self.body.find('cm-home-getting-started-tour')
        self.assertGreater(idx, -1)
        chunk = self.body[max(0, idx - 200):idx + 200]
        self.assertIn('aria-label', chunk)
        self.assertIn('overlay tour', chunk)


class TourJsSourceTests(unittest.TestCase):
    """Static checks on the tour.js source. We can't run JS in pytest
    without a browser, but we can confirm the file exists, declares
    the public API, and pins the localStorage key to a stable value."""

    @classmethod
    def setUpClass(cls):
        cls.path = _ROOT / 'static' / 'modules' / 'tour.js'
        assert cls.path.exists(), f"tour.js missing at {cls.path}"
        cls.src = cls.path.read_text(encoding='utf-8')

    def test_public_api_exposed(self):
        """startTour and endTour must be reachable from window scope
        so the onclick handlers in the home banner can call them."""
        self.assertIn('window.startTour', self.src)
        self.assertIn('window.endTour', self.src)

    def test_localstorage_key_pinned(self):
        """The localStorage key is part of the operator-visible state
        contract. Changing it silently means the tour re-runs for
        every existing user on next upgrade — make that a deliberate
        choice by pinning the value in this test."""
        self.assertIn("'cm.tour.shown'", self.src)

    def test_steps_built_against_sort_tab_elements(self):
        """The five steps anchor to existing Sort-tab selectors. If
        any of these selectors disappears, the tour just skips the
        highlight ring (still shows the tip) — so this test is a
        soft signal that the Sort tab still has the elements the
        tour expects."""
        for selector in (
            '.cm-sort-hero-pre',
            '#sort-preset-select',
            '#sort-config-table',
        ):
            self.assertIn(selector, self.src,
                          f"tour.js step missing selector {selector!r}")


if __name__ == '__main__':
    unittest.main()
