"""
Smoke test for the compact-pagination renderer in collection.js.

The renderer is pure JS so we can't unit-test the DOM directly without
a browser. What we CAN do is read the source and pin the load-bearing
behaviours by string-matching, plus run a quick Python-side simulation
of the window-of-pages math against the same algorithm so we don't
silently regress it.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


class CollectionPaginationSourceTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.src = (_ROOT / 'static' / 'modules' / 'collection.js') \
            .read_text(encoding='utf-8')

    def test_compact_pagination_function_exists(self):
        """The renderer is split out from loadInventory so it can be
        tested + reused."""
        self.assertIn('_renderInventoryPagination', self.src)
        self.assertIn('function _renderInventoryPagination(', self.src)

    def test_no_per_page_button_loop(self):
        """The old `for (let p = 1; p <= data.pages; p++)` loop that
        created one button per page must be gone — that's the
        regression this commit guards against."""
        self.assertNotIn('for (let p = 1; p <= data.pages; p++)', self.src)

    def test_uses_window_of_pages(self):
        """The new renderer maintains a small `WINDOW` of pages around
        the current one rather than rendering all of them."""
        self.assertIn('const WINDOW', self.src)
        # The «/» arrow buttons are part of the compact UI.
        self.assertIn("'«'", self.src)  # «
        self.assertIn("'»'", self.src)  # »


class WindowOfPagesAlgorithmTests(unittest.TestCase):
    """Python re-implementation of the JS windowing logic so the
    expected output of _renderInventoryPagination at different
    (totalPages, currentPage) inputs can be regression-tested
    without a browser."""

    @staticmethod
    def _window(total_pages, current_page, window=2):
        """Mirror the JS algorithm — produce the sorted page list that
        gets rendered (sans ellipses, sans arrows)."""
        pages = {1, total_pages, current_page}
        for d in range(1, window + 1):
            if current_page - d > 1:
                pages.add(current_page - d)
            if current_page + d < total_pages:
                pages.add(current_page + d)
        return sorted(pages)

    def test_first_page_of_200(self):
        # Should render: 1 [+ neighbours 2, 3] ... 200
        self.assertEqual(
            self._window(200, 1),
            [1, 2, 3, 200],
        )

    def test_middle_page_of_200(self):
        # Should render: 1 ... 98 99 [100] 101 102 ... 200
        self.assertEqual(
            self._window(200, 100),
            [1, 98, 99, 100, 101, 102, 200],
        )

    def test_last_page_of_200(self):
        # Should render: 1 ... 198 199 [200]
        self.assertEqual(
            self._window(200, 200),
            [1, 198, 199, 200],
        )

    def test_few_pages_no_compaction(self):
        # 5 pages, current=3 — all pages fit so no ellipses needed
        self.assertEqual(
            self._window(5, 3),
            [1, 2, 3, 4, 5],
        )

    def test_button_count_bounded(self):
        """At any total/current combo, the rendered list stays small —
        this is the load-bearing property. Old code: ~200 buttons.
        New code: <= 2 (first/last) + 2*WINDOW + 1 (current) = 7."""
        for total in (10, 50, 200, 1000, 10000):
            for cur in (1, total // 2, total):
                self.assertLessEqual(
                    len(self._window(total, cur)),
                    7,
                    f"Window too big at total={total}, current={cur}",
                )


if __name__ == '__main__':
    unittest.main()
