"""Unit tests for fetch_set_symbols.py.

Network is not exercised. Tests run against the on-disk cache produced by
an earlier `python fetch_set_symbols.py` run, plus a tiny in-memory SVG
for the rasterization path.

If `card_data/set_symbols/svg/xln.svg` is missing, the on-disk tests
skip — they're integration-style checks, not preconditions for unit
CI.
"""

import json
import os
import sys
import tempfile
import textwrap
import unittest

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_set_symbols as fss


_TINY_SVG = textwrap.dedent("""\
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
      <rect x="10" y="10" width="80" height="80" fill="#000000"/>
    </svg>
""").encode("utf-8")


class TestMonochromeNormalize(unittest.TestCase):
    def test_inverts_luminance(self):
        # Black pixel -> 255 (icon), white pixel -> 0 (bg)
        rgb = np.array([[[0, 0, 0], [255, 255, 255]]], dtype=np.uint8)
        mono = fss.monochrome_normalize(rgb)
        self.assertEqual(mono.shape, (1, 2))
        self.assertEqual(mono[0, 0], 255)
        self.assertEqual(mono[0, 1], 0)

    def test_accepts_grayscale_input(self):
        gray = np.array([[0, 128, 255]], dtype=np.uint8)
        mono = fss.monochrome_normalize(gray)
        np.testing.assert_array_equal(mono, np.array([[255, 127, 0]]))


class TestRasterizeSvg(unittest.TestCase):
    def test_rasterizes_square_svg_to_requested_height(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "tiny.svg")
            with open(path, "wb") as f:
                f.write(_TINY_SVG)
            rgb = fss.rasterize_svg(path, size=48)
        self.assertIsNotNone(rgb)
        self.assertEqual(rgb.shape[0], 48)
        self.assertEqual(rgb.shape[1], 48)
        self.assertEqual(rgb.shape[2], 3)

    def test_returns_none_for_unparseable(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "broken.svg")
            with open(path, "wb") as f:
                f.write(b"not an svg")
            self.assertIsNone(fss.rasterize_svg(path, size=48))


class TestMakeEdgeTemplate(unittest.TestCase):
    def test_edges_nonzero_for_shape(self):
        mono = np.zeros((48, 48), dtype=np.uint8)
        mono[10:40, 10:40] = 255
        edges = fss.make_edge_template(mono)
        self.assertGreater(int(np.count_nonzero(edges)), 0)

    def test_edges_zero_for_flat_image(self):
        mono = np.full((48, 48), 128, dtype=np.uint8)
        edges = fss.make_edge_template(mono)
        self.assertEqual(int(np.count_nonzero(edges)), 0)


class TestConfusablePairs(unittest.TestCase):
    def test_detects_identical_rasters(self):
        with tempfile.TemporaryDirectory() as td:
            img = np.zeros((48, 48), dtype=np.uint8)
            img[10:40, 10:40] = 255
            cv2.imwrite(os.path.join(td, "alpha_48.png"), img)
            cv2.imwrite(os.path.join(td, "beta_48.png"), img.copy())
            cv2.imwrite(os.path.join(td, "gamma_48.png"),
                        np.random.RandomState(0).randint(0, 256, (48, 48),
                                                         dtype=np.uint8))
            pairs = fss.compute_confusable_pairs(png_dir=td, size=48,
                                                 threshold=4)
        # alpha/beta should match with distance 0
        self.assertIn(("alpha", "beta", 0), pairs)
        self.assertTrue(all(d <= 4 for _, _, d in pairs))

    def test_sorted_by_distance_then_name(self):
        with tempfile.TemporaryDirectory() as td:
            a = np.zeros((48, 48), dtype=np.uint8)
            b = a.copy(); b[10:40, 10:40] = 255
            c = b.copy(); c[30:35, 30:35] = 0  # slightly different
            cv2.imwrite(os.path.join(td, "a_48.png"), a)
            cv2.imwrite(os.path.join(td, "b_48.png"), b)
            cv2.imwrite(os.path.join(td, "c_48.png"), c)
            pairs = fss.compute_confusable_pairs(png_dir=td, size=48,
                                                 threshold=64)
        dists = [d for _, _, d in pairs]
        self.assertEqual(dists, sorted(dists))


class TestOnDiskCache(unittest.TestCase):
    """Sanity checks on the cache built by a real pipeline run.

    Skipped when no cache is present so these tests don't force every CI
    run to hit Scryfall. Run `python fetch_set_symbols.py` once locally
    to produce the cache.
    """

    def setUp(self):
        self.xln_svg = os.path.join(fss.SVG_DIR, "xln.svg")
        if not os.path.exists(self.xln_svg):
            self.skipTest("set_symbols cache not populated — "
                          "run fetch_set_symbols.py first")

    def test_all_raster_sizes_present_for_cached_set(self):
        for size in fss.RASTER_SIZES:
            for subdir in (fss.PNG_DIR, fss.EDGE_DIR):
                path = os.path.join(subdir, f"xln_{size}.png")
                self.assertTrue(os.path.exists(path),
                                f"missing {os.path.relpath(path)}")

    def test_confusable_report_valid_json(self):
        if not os.path.exists(fss.CONFUSABLE_PATH):
            self.skipTest("no confusable_pairs.json — pipeline hasn't run")
        with open(fss.CONFUSABLE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
        self.assertIsInstance(payload, list)
        if payload:
            sample = payload[0]
            self.assertIn("set_a", sample)
            self.assertIn("set_b", sample)
            self.assertIn("phash_distance", sample)

    def test_rasterized_icon_has_signal(self):
        mono = cv2.imread(os.path.join(fss.PNG_DIR, "xln_48.png"),
                          cv2.IMREAD_GRAYSCALE)
        self.assertIsNotNone(mono)
        icon_px = int(np.count_nonzero(mono > 50))
        # An icon should have at least 50 bright pixels in a 48x48 frame
        self.assertGreater(icon_px, 50)


if __name__ == "__main__":
    unittest.main()
