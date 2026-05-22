"""
Tests for the cm-help-btn popover system + the Tome of Knowledge stub.

The help popovers are pure JS so we can't directly assert popover
behaviour without a browser. We instead pin:
  1. The Jinja `help_btn` macro renders the right HTML attributes
     (data-bs-toggle, data-cm-help-id, etc.) so Bootstrap will
     promote the <button> to a popover at runtime.
  2. Every popover authored in the templates carries a stable
     data-cm-help-id (load-bearing for telemetry / Tome-anchor
     matching).
  3. The Tome route returns 200 and includes the anchor sections
     popovers may link to.
  4. The Tome covers every tome_anchor= value referenced from a
     help_btn invocation across the template tree.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import web_server


class HelpButtonMarkupTests(unittest.TestCase):
    """Render index.html and the modals partial, then walk every
    rendered cm-help-btn for the required attributes."""

    @classmethod
    def setUpClass(cls):
        cls.client = web_server.app.test_client()
        r = cls.client.get('/')
        assert r.status_code == 200
        cls.body = r.get_data(as_text=True)

    def test_help_js_loaded(self):
        """The bundle pulls in modules/help.js — Bootstrap popovers
        won't render without it."""
        self.assertIn('/static/modules/help.js', self.body)

    def test_at_least_one_help_button(self):
        """If no popovers were rendered, either the macro silently
        broke or every help_btn invocation was removed by mistake."""
        self.assertIn('class="cm-help-btn"', self.body,
                      "No cm-help-btn rendered — help system AWOL?")

    def test_help_buttons_carry_required_attrs(self):
        """Every cm-help-btn carries data-bs-toggle="popover" AND
        a data-cm-help-id. Without both, Bootstrap won't promote it
        and the JS won't track it."""
        # Find every <button class="cm-help-btn" ...>...</button>.
        buttons = re.findall(
            r'<button[^>]*class="cm-help-btn"[^>]*>',
            self.body,
        )
        self.assertGreater(len(buttons), 0)
        for tag in buttons:
            self.assertIn('data-bs-toggle="popover"', tag,
                          f"Help button missing popover toggle: {tag}")
            self.assertIn('data-cm-help-id="', tag,
                          f"Help button missing data-cm-help-id: {tag}")
            self.assertIn('aria-label="Help:', tag,
                          f"Help button missing aria-label: {tag}")

    def test_help_button_ids_unique(self):
        """Every help button has a unique data-cm-help-id. Duplicate
        ids would make the planned telemetry hook ambiguous and
        break the Tome-link convention."""
        ids = re.findall(
            r'data-cm-help-id="([^"]+)"',
            self.body,
        )
        self.assertEqual(
            sorted(ids), sorted(set(ids)),
            f"Duplicate help IDs: "
            f"{[i for i in ids if ids.count(i) > 1]}",
        )


class TomeRouteTests(unittest.TestCase):
    """The Tome of Knowledge stub page."""

    def setUp(self):
        self.client = web_server.app.test_client()

    def test_tome_route_returns_200(self):
        r = self.client.get('/tome')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'Tome of Knowledge', r.data)

    def test_tome_lists_seeded_anchors(self):
        """The TOC + section bodies reference every anchor the help
        popovers expect. If a tome_anchor= value in a help_btn
        invocation falls out of sync, the corresponding 'Read more'
        link 404s to that section."""
        r = self.client.get('/tome')
        body = r.get_data(as_text=True)
        for anchor in (
            'query-syntax', 'bin-overrides', 'fallback-bin',
            'continuous-mode', 'card-limit', 'aruco-calibration',
            'drop-tuner', 'source-bin-empty', 'detection-review',
            'enrichment-sources', 'card-data-update', 'estop-recovery',
            'presets', 'moxfield', 'wishlist-bin', 'boxes-dividers',
        ):
            self.assertIn(f'id="{anchor}"', body,
                          f"Tome missing seeded anchor {anchor!r}")

    def test_tome_back_link_to_kiosk(self):
        """The Tome offers a way back to the operator UI."""
        r = self.client.get('/tome')
        body = r.get_data(as_text=True)
        self.assertIn('Back to the kiosk', body)
        self.assertIn('href="/"', body)


class TomeAnchorCoverageTests(unittest.TestCase):
    """For every tome_anchor= value referenced from a help_btn in
    any template, assert the Tome page actually renders that
    section. Catches the case where someone adds a help popover
    pointing at /tome#new-anchor without adding the corresponding
    section to tome.html."""

    @classmethod
    def setUpClass(cls):
        # Scan all template partials for tome_anchor="X" arguments.
        tmpl_dir = _ROOT / 'templates'
        cls.referenced = set()
        anchor_re = re.compile(r'tome_anchor=["\']([a-z0-9\-]+)["\']')
        for path in tmpl_dir.rglob('*.html'):
            for m in anchor_re.finditer(path.read_text(encoding='utf-8')):
                cls.referenced.add(m.group(1))

        client = web_server.app.test_client()
        r = client.get('/tome')
        cls.tome_body = r.get_data(as_text=True)

    def test_every_referenced_anchor_has_a_section(self):
        missing = [
            a for a in self.referenced
            if f'id="{a}"' not in self.tome_body
        ]
        self.assertEqual(
            missing, [],
            f"help_btn(tome_anchor=...) references {missing} but the "
            f"Tome page has no matching <section id='...'>. Add the "
            f"section to templates/tome.html.",
        )


if __name__ == '__main__':
    unittest.main()
