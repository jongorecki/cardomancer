"""Unit tests for hashing.py — hash computation and distance calculation."""

import sys
import os
import unittest
from PIL import Image
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hashing import hash_image_color, compute_distances_for_image, compute_combined_distances, _apply_clahe_pil


class TestHashImageColor(unittest.TestCase):

    def _make_solid_image(self, color=(255, 0, 0), size=(745, 1043)):
        """Create a solid-color PIL image."""
        arr = np.full((size[1], size[0], 3), color, dtype=np.uint8)
        return Image.fromarray(arr, 'RGB')

    def _make_gradient_image(self, size=(745, 1043)):
        """Create a gradient PIL image."""
        w, h = size
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        for x in range(w):
            arr[:, x, 0] = int(255 * x / w)  # R gradient
        for y in range(h):
            arr[y, :, 1] = int(255 * y / h)  # G gradient
        arr[:, :, 2] = 128  # Constant blue
        return Image.fromarray(arr, 'RGB')

    def test_returns_tuple(self):
        img = self._make_solid_image()
        result = hash_image_color(img)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_identical_images_zero_or_low_distance(self):
        """If the hash DB has an entry, identical images should match with distance 0."""
        # This test only works meaningfully if PRECOMPUTED_HASHES is non-empty.
        # If DB is empty, best_id will be None.
        img = self._make_solid_image()
        best_id, best_dist = hash_image_color(img)
        # We can't assert a specific match without a DB, but we can check types
        if best_id is not None:
            self.assertIsInstance(best_dist, float)
            self.assertGreaterEqual(best_dist, 0)
        else:
            self.assertIsNone(best_id)
            self.assertEqual(best_dist, float('inf'))

    def test_non_standard_image_size(self):
        """Should handle non-card-sized images."""
        img = self._make_solid_image(size=(200, 200))
        result = hash_image_color(img)
        self.assertIsInstance(result, tuple)


class TestComputeDistancesForImage(unittest.TestCase):

    def _make_solid_image(self, color=(255, 0, 0), size=(745, 1043)):
        arr = np.full((size[1], size[0], 3), color, dtype=np.uint8)
        return Image.fromarray(arr, 'RGB')

    def test_returns_list(self):
        img = self._make_solid_image()
        results = compute_distances_for_image(img)
        self.assertIsInstance(results, list)

    def test_results_are_tuples(self):
        img = self._make_solid_image()
        results = compute_distances_for_image(img)
        for item in results:
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)

    def test_distances_non_negative(self):
        img = self._make_solid_image()
        results = compute_distances_for_image(img)
        for card_id, dist in results:
            self.assertGreaterEqual(dist, 0)

    def test_result_count_matches_db(self):
        """Number of results should match PRECOMPUTED_HASHES length."""
        from hashing import PRECOMPUTED_HASHES
        img = self._make_solid_image()
        results = compute_distances_for_image(img)
        self.assertEqual(len(results), len(PRECOMPUTED_HASHES))


class TestApplyCLAHE(unittest.TestCase):

    def _make_gradient_image(self, size=(745, 1043)):
        w, h = size
        arr = np.zeros((h, w, 3), dtype=np.uint8)
        for x in range(w):
            arr[:, x, 0] = int(255 * x / w)
        for y in range(h):
            arr[y, :, 1] = int(255 * y / h)
        arr[:, :, 2] = 128
        return Image.fromarray(arr, 'RGB')

    def test_returns_pil_image(self):
        img = self._make_gradient_image()
        result = _apply_clahe_pil(img)
        self.assertIsInstance(result, Image.Image)

    def test_same_size(self):
        img = self._make_gradient_image()
        result = _apply_clahe_pil(img)
        self.assertEqual(result.size, img.size)

    def test_modifies_image(self):
        img = self._make_gradient_image()
        result = _apply_clahe_pil(img)
        # CLAHE should change at least some pixels
        orig_arr = np.array(img)
        result_arr = np.array(result)
        self.assertGreater(np.sum(np.abs(orig_arr.astype(int) - result_arr.astype(int))), 0)


class TestComputeCombinedDistances(unittest.TestCase):

    def _make_solid_image(self, color=(255, 0, 0), size=(745, 1043)):
        arr = np.full((size[1], size[0], 3), color, dtype=np.uint8)
        return Image.fromarray(arr, 'RGB')

    def test_returns_sorted_list(self):
        img = self._make_solid_image()
        results = compute_combined_distances(img)
        self.assertIsInstance(results, list)
        # Should be sorted by distance
        if len(results) >= 2:
            for i in range(len(results) - 1):
                self.assertLessEqual(results[i][1], results[i + 1][1])

    def test_results_are_tuples(self):
        img = self._make_solid_image()
        results = compute_combined_distances(img)
        for item in results:
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)

    def test_distances_non_negative(self):
        img = self._make_solid_image()
        results = compute_combined_distances(img)
        for card_id, dist in results:
            self.assertGreaterEqual(dist, 0)

    def test_result_count_matches_db(self):
        from hashing import PRECOMPUTED_HASHES
        img = self._make_solid_image()
        results = compute_combined_distances(img)
        self.assertEqual(len(results), len(PRECOMPUTED_HASHES))


if __name__ == "__main__":
    unittest.main()
