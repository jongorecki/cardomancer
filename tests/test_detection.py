"""Unit tests for detection.py — perspective correction, deskew, bounding box save/load."""

import sys
import os
import json
import tempfile
import unittest
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
# Patch config before importing detection so it doesn't fail on missing files
import config
import detection as detection_mod

from detection import (
    get_perspective_corrected_card,
    contours_are_similar,
    scale_corners,
    save_bounding_box,
    load_bounding_box,
    deskew_card,
)

_original_bb_path = detection_mod.BOUNDING_BOX_PATH


class TestGetPerspectiveCorrectedCard(unittest.TestCase):

    def _make_frame_with_rect(self, width=800, height=600):
        """Create a test frame with a known rectangular region."""
        frame = np.zeros((height, width, 3), dtype=np.uint8)
        # Draw a white rectangle
        cv2.rectangle(frame, (100, 100), (400, 500), (255, 255, 255), -1)
        return frame

    def test_output_shape(self):
        frame = self._make_frame_with_rect()
        approx = np.array([
            [[100, 100]],
            [[400, 100]],
            [[400, 500]],
            [[100, 500]]
        ], dtype=np.float32)
        result = get_perspective_corrected_card(frame, approx, width=745, height=1043)
        self.assertEqual(result.shape, (1043, 745, 3))

    def test_output_portrait(self):
        """Result should always be portrait orientation."""
        frame = self._make_frame_with_rect(800, 600)
        # Landscape-oriented contour (wider than tall)
        approx = np.array([
            [[50, 200]],
            [[700, 200]],
            [[700, 400]],
            [[50, 400]]
        ], dtype=np.float32)
        result = get_perspective_corrected_card(frame, approx, width=745, height=1043)
        h, w = result.shape[:2]
        self.assertGreater(h, w, "Output should be portrait (height > width)")

    def test_custom_dimensions(self):
        frame = self._make_frame_with_rect()
        approx = np.array([
            [[100, 100]],
            [[400, 100]],
            [[400, 500]],
            [[100, 500]]
        ], dtype=np.float32)
        result = get_perspective_corrected_card(frame, approx, width=300, height=400)
        self.assertEqual(result.shape, (400, 300, 3))


class TestContoursAreSimilar(unittest.TestCase):

    def test_identical_contours(self):
        c = np.array([[[0, 0]], [[100, 0]], [[100, 100]], [[0, 100]]])
        self.assertTrue(contours_are_similar(c, c))

    def test_similar_contours(self):
        c1 = np.array([[[0, 0]], [[100, 0]], [[100, 100]], [[0, 100]]])
        c2 = np.array([[[0, 0]], [[101, 0]], [[101, 101]], [[0, 101]]])
        self.assertTrue(contours_are_similar(c1, c2, tolerance=0.05))

    def test_different_contours(self):
        c1 = np.array([[[0, 0]], [[100, 0]], [[100, 100]], [[0, 100]]])
        c2 = np.array([[[0, 0]], [[500, 0]], [[500, 500]], [[0, 500]]])
        self.assertFalse(contours_are_similar(c1, c2, tolerance=0.01))


class TestScaleCorners(unittest.TestCase):

    def test_no_scale(self):
        corners = [(0, 0), (100, 0), (100, 100), (0, 100)]
        result = scale_corners(corners, 1.0)
        for (rx, ry), (ex, ey) in zip(result, corners):
            self.assertAlmostEqual(rx, ex, places=3)
            self.assertAlmostEqual(ry, ey, places=3)

    def test_half_scale(self):
        corners = [(0, 0), (100, 0), (100, 100), (0, 100)]
        result = scale_corners(corners, 0.5)
        # Center is (50, 50); half scale means each corner is halfway to center
        expected = [(25, 25), (75, 25), (75, 75), (25, 75)]
        for (rx, ry), (ex, ey) in zip(result, expected):
            self.assertAlmostEqual(rx, ex, places=3)
            self.assertAlmostEqual(ry, ey, places=3)

    def test_zero_scale(self):
        corners = [(0, 0), (100, 0), (100, 100), (0, 100)]
        result = scale_corners(corners, 0.0)
        # All corners should collapse to center
        for rx, ry in result:
            self.assertAlmostEqual(rx, 50, places=3)
            self.assertAlmostEqual(ry, 50, places=3)


class TestBoundingBoxSaveLoad(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.bb_path = os.path.join(self.tmpdir, "bounding_box.json")
        detection_mod.BOUNDING_BOX_PATH = self.bb_path

    def tearDown(self):
        detection_mod.BOUNDING_BOX_PATH = _original_bb_path
        if os.path.exists(self.bb_path):
            os.remove(self.bb_path)
        os.rmdir(self.tmpdir)

    def test_save_and_load(self):
        corners = np.array([[10, 20], [300, 20], [300, 400], [10, 400]], dtype=np.float32)
        save_bounding_box(corners)
        loaded = load_bounding_box()
        self.assertIsNotNone(loaded)
        np.testing.assert_array_almost_equal(loaded, corners)

    def test_load_missing_file(self):
        result = load_bounding_box()
        self.assertIsNone(result)

    def test_load_invalid_json(self):
        with open(self.bb_path, 'w') as f:
            f.write("not json")
        result = load_bounding_box()
        self.assertIsNone(result)

    def test_load_wrong_shape(self):
        with open(self.bb_path, 'w') as f:
            json.dump([[1, 2, 3], [4, 5, 6]], f)
        result = load_bounding_box()
        self.assertIsNone(result)


class TestDeskewCard(unittest.TestCase):

    def _make_card_image(self, angle=0, width=745, height=1043):
        """Create a synthetic card image with known edges, optionally rotated."""
        img = np.full((height, width, 3), 200, dtype=np.uint8)
        # Draw strong border lines
        cv2.rectangle(img, (20, 20), (width - 20, height - 20), (0, 0, 0), 3)
        # Draw horizontal lines (like text baselines)
        for y in range(100, height - 100, 80):
            cv2.line(img, (40, y), (width - 40, y), (0, 0, 0), 2)

        if abs(angle) > 0.01:
            center = (width // 2, height // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            img = cv2.warpAffine(img, M, (width, height),
                                  borderMode=cv2.BORDER_REPLICATE)
        return img

    def test_no_deskew_for_straight_image(self):
        img = self._make_card_image(angle=0)
        result = deskew_card(img)
        # Should return same or very similar image
        self.assertEqual(result.shape, img.shape)

    def test_deskew_corrects_moderate_skew(self):
        """A 3° rotated image should be corrected."""
        img = self._make_card_image(angle=3.0)
        result = deskew_card(img)
        self.assertEqual(result.shape, img.shape)
        # The result should differ from the input (it was corrected)
        # We can't easily verify exact angle, but we know the function ran
        diff = cv2.absdiff(img, result)
        self.assertGreater(np.sum(diff), 0, "Deskew should modify a skewed image")

    def test_ignores_tiny_skew(self):
        """Skew < 0.3° should be ignored."""
        img = self._make_card_image(angle=0.1)
        result = deskew_card(img)
        # Should be identical to input (skew too small to correct)
        np.testing.assert_array_equal(result, img)

    def test_ignores_large_angle(self):
        """Skew > max_angle should be ignored."""
        img = self._make_card_image(angle=15.0)
        result = deskew_card(img, max_angle=7)
        # Should return the original (angle too large)
        self.assertEqual(result.shape, img.shape)


if __name__ == "__main__":
    unittest.main()
