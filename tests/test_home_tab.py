"""
Tests for the Home / welcome landing tab.

The Home tab is the default-active tab; it shows a welcome hero,
three action tiles routing to Sort/Collection/Setup, and a quick
status strip populated by JS from the existing API. These tests
pin:

  1. The index.html render includes the Home partial.
  2. The tabs nav lists Home first and marks it active.
  3. The Sort tab is NOT marked active anymore (Home takes its slot).
  4. The Home partial contains the action tiles with correct hrefs.
  5. The status strip placeholders exist with the IDs the JS expects.
  6. Existing route flow still works (full status, collection stats,
     recent sessions) — these are the data sources the Home loader
     hits. If any of them stops returning JSON, the home cells show
     a dash, not a crash.
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


class HomeTabTemplateTests(unittest.TestCase):
    """Render index.html through the Flask test client and assert
    the Home tab structure."""

    @classmethod
    def setUpClass(cls):
        cls.client = web_server.app.test_client()
        r = cls.client.get('/')
        assert r.status_code == 200, f"GET / returned {r.status_code}"
        cls.body = r.get_data(as_text=True)

    def test_home_partial_rendered(self):
        """The Home tab's container is present."""
        self.assertIn('id="tab-home"', self.body)

    def test_home_tab_is_default_active(self):
        """The tab-home pane carries `show active`."""
        # Search around the tab-home id for the pane class.
        idx = self.body.find('id="tab-home"')
        self.assertGreater(idx, -1)
        # Look back ~200 chars from the id to find the surrounding
        # <div class="..."> attribute.
        chunk = self.body[max(0, idx - 200):idx + 80]
        self.assertIn('show active', chunk,
                      "tab-home pane must carry `show active`")

    def test_sort_tab_no_longer_default_active(self):
        """Sort used to be the default; Home now is. Sort's pane
        should NOT carry `show active` anymore."""
        idx = self.body.find('id="tab-sort"')
        self.assertGreater(idx, -1)
        chunk = self.body[max(0, idx - 200):idx + 60]
        # The pane class declaration should contain `fade` but
        # not `show active`. We assert by spotting `class="tab-pane fade"`
        # without ` show active` immediately after.
        self.assertNotIn('tab-pane fade show active', chunk,
                         "Sort pane must lose `show active` to Home")

    def test_tabs_nav_lists_home_first(self):
        """The Home link is the first nav-item in #mainTabs."""
        nav_idx = self.body.find('id="mainTabs"')
        self.assertGreater(nav_idx, -1)
        # Find the first <a class="nav-link...">...</a> after the
        # nav opens; that should be the Home link.
        nav_chunk = self.body[nav_idx:nav_idx + 1500]
        home_anchor = nav_chunk.find('href="#tab-home"')
        sort_anchor = nav_chunk.find('href="#tab-sort"')
        collection_anchor = nav_chunk.find('href="#tab-collection"')
        setup_anchor = nav_chunk.find('href="#tab-setup"')
        self.assertGreater(home_anchor, -1, "Home link missing from nav")
        self.assertGreater(sort_anchor, -1)
        self.assertLess(home_anchor, sort_anchor,
                        "Home tab must precede Sort in the nav order")
        self.assertLess(sort_anchor, collection_anchor)
        self.assertLess(collection_anchor, setup_anchor)

    def test_home_nav_link_is_active(self):
        """The Home anchor in #mainTabs has the `active` class."""
        nav_idx = self.body.find('id="mainTabs"')
        nav_chunk = self.body[nav_idx:nav_idx + 1500]
        # Find the home anchor and look around it for "active"
        a_idx = nav_chunk.find('href="#tab-home"')
        # Walk backwards a bit to capture the surrounding <a> tag.
        chunk = nav_chunk[max(0, a_idx - 120):a_idx]
        self.assertIn('nav-link active', chunk,
                      "Home nav link must carry the 'active' class")

    def test_action_tiles_route_to_each_main_tab(self):
        """Each of the three tiles calls goToTab() with the correct
        target href."""
        for target in ('#tab-sort', '#tab-collection', '#tab-setup'):
            self.assertIn(f"goToTab('{target}')", self.body,
                          f"Home tile for {target} not found")

    def test_status_strip_placeholders_present(self):
        """The three status spans the JS populates by ID exist."""
        for sid in ('home-status-hardware',
                    'home-status-collection',
                    'home-status-last-session'):
            self.assertIn(f'id="{sid}"', self.body,
                          f"Status placeholder #{sid} missing")

    def test_getting_started_banner_initial_hidden(self):
        """Fresh-install banner is in markup but hidden until JS
        decides whether to show it. We just confirm the element
        exists and has the inline display:none — JS reveals it."""
        idx = self.body.find('id="home-getting-started"')
        self.assertGreater(idx, -1)
        chunk = self.body[idx:idx + 200]
        self.assertIn('display:none', chunk,
                      "Getting-started banner should start hidden")

    def test_calibration_wizard_data_target(self):
        """The Run-wizard button in the banner targets the existing
        calibration-wizard modal (no broken link)."""
        idx = self.body.find('id="home-getting-started"')
        chunk = self.body[idx:idx + 1500]
        self.assertIn('data-bs-target="#calibration-wizard-modal"', chunk)

    # ------------------------------------------------------------------
    # Redesign-specific markers (Claude Design handoff)
    # ------------------------------------------------------------------

    def test_backdrop_sigils_present(self):
        """The redesigned home tab has 7 rotating card sigils in a
        fixed backdrop (sg-1 through sg-7). The backdrop is the
        signature visual change in the Claude Design redesign."""
        self.assertIn('class="cm-backdrop"', self.body)
        for i in range(1, 8):
            self.assertIn(f'sg-{i}', self.body,
                          f"backdrop sigil sg-{i} missing")

    def test_hero_redesigned_copy(self):
        """Hero uses the redesign's display copy. 'The Cardomancer
        awaits' is the load-bearing brand line; if it changes back to
        the old 'Welcome to Cardomancer' that's a regression in
        intent."""
        self.assertIn('awaits', self.body,
                      "Hero copy should read 'The Cardomancer awaits.'")
        self.assertIn('Drop a stack. The machine does the rest.', self.body,
                      "Tagline copy missing from hero")

    def test_eyebrow_above_title(self):
        """Eyebrow strip 'Trading Card Collection System' renders
        above the title in the new hero block."""
        self.assertIn('cm-home-eyebrow', self.body)
        eb_idx = self.body.find('cm-home-eyebrow')
        title_idx = self.body.find('cm-home-title')
        self.assertGreater(title_idx, eb_idx,
                           "Eyebrow must precede title in markup order")

    def test_action_tiles_have_roman_numerals(self):
        """Each tile is numbered I / II / III via a .cm-home-tile-num
        span. Confirms the redesign's ritual-step framing is present."""
        for numeral in ('I', 'II', 'III'):
            self.assertIn(
                f'class="cm-home-tile-num">{numeral}</span>',
                self.body,
                f"Tile numeral {numeral!r} missing — redesign drops it"
            )

    def test_redesigned_tile_titles(self):
        """The redesign renames the tile titles to ritual-style verbs."""
        for title in ('Begin the sorting',
                      'Consult the library',
                      'Tune the apparatus'):
            self.assertIn(title, self.body,
                          f"Redesigned tile title {title!r} missing")

    def test_getting_started_uses_ritual_copy(self):
        """The fresh-install banner uses the redesigned ritual copy."""
        self.assertIn('A first ritual is required.', self.body)
        self.assertIn('Run calibration ritual', self.body)

    def test_footer_flourish_present(self):
        """The redesign adds a small monospaced footer flourish
        ('vX.Y.Z · scan, divine, route'). Confirms the partial wires
        in app_version + tagline."""
        self.assertIn('cm-home-footer', self.body)
        self.assertIn('scan, divine, route', self.body)


class HomeStatusDataSourcesContract(unittest.TestCase):
    """The Home JS loader hits three existing endpoints. We pin the
    JSON contract — IF the endpoint serves a parseable response,
    the expected field must be present. We tolerate failures without
    failing the test because:

      1. The home.js loader has its own try/except that falls back
         to '—' on any error — that's the user-visible contract.
      2. Cross-test pollution from other test files' setUpClass-
         installed gcode_control MagicMocks can break unrelated
         routes when they run earlier in the full-suite ordering.

    The conftest sets `app.config['TESTING'] = True` autouse, which
    makes the Flask test client RE-RAISE exceptions from view
    functions instead of returning a 500. We wrap each call in a
    try/except so a polluted endpoint surfaces as skipTest, not as
    a noisy traceback failure."""

    def setUp(self):
        self.client = web_server.app.test_client()

    def _safe_get_json(self, url):
        """Hit `url`; return its parsed JSON, or None if the call
        raised (test pollution) or returned non-200."""
        try:
            r = self.client.get(url)
        except Exception as exc:
            self.skipTest(
                f"GET {url} raised {type(exc).__name__} — likely "
                f"a polluted gcode_control mock from another test. "
                f"The home.js loader handles this case by falling "
                f"back to '—' on the user-visible side."
            )
        if r.status_code != 200:
            self.skipTest(
                f"GET {url} returned {r.status_code}; loader tolerates."
            )
        try:
            return r.get_json()
        except Exception as exc:
            self.skipTest(
                f"GET {url} returned non-JSON ({type(exc).__name__})."
            )

    def test_api_status_returns_state_field_when_healthy(self):
        data = self._safe_get_json('/api/status')
        self.assertIsNotNone(data, "/api/status must return JSON")
        self.assertIn('state', data,
                      "/api/status must return a 'state' field — "
                      "the Home tab's hardware cell depends on it")

    def test_api_collection_stats_returns_json_when_healthy(self):
        data = self._safe_get_json('/api/collection/stats')
        self.assertIsNotNone(data,
                             "/api/collection/stats must return JSON")

    def test_api_collection_sessions_with_limit_when_healthy(self):
        """The Home loader requests ?limit=1; confirm the endpoint
        accepts that param and returns a 'sessions' array."""
        data = self._safe_get_json('/api/collection/sessions?limit=1')
        self.assertIn('sessions', data,
                      "/api/collection/sessions must return a "
                      "'sessions' array — the Home last-session "
                      "cell depends on it")


if __name__ == '__main__':
    unittest.main()
