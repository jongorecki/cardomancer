"""Unit tests for foil_detect.py — multi-signal foil detection.

We synthesize card + reference pairs to verify that the detector:
  - Computes the three signal deltas correctly from known inputs
  - Degrades gracefully when inputs are missing or malformed
  - Reproduces a pinned score for fixed signal inputs (regression guard
    against accidental weight / bias / threshold changes)

Note on synthetic "foils": the _make_foil_scan_from_reference fixture
generates OLD-LIGHTING foil physics (brighter, desaturated hotspots).
That's useful for testing signal direction but does NOT match what
real foils look like under the current LED scanner (where foils read
darker with saturated rainbow hotspots). Do not use the synthesizer
to validate whether the classifier fires correctly — use the pinned
regression test for that.
"""

import sys
import os
import unittest

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import foil_detect
from foil_detect import (
    detect_foil,
    _compute_bright_stats,
    _load_reference,
    _load_reference_back,
    FOIL_CONFIDENCE_THRESHOLD,
    FOIL_BIAS,
    W_DELTA_BRIGHT_FRAC,
    W_DELTA_MEAN_S,
    W_DELTA_N_BRIGHT_CLUSTERS,
    W_DELTA_STD_S_BRIGHT,
    W_DELTA_LAPLACIAN_ENERGY,
    BRIGHT_V_THRESH,
    MIN_BRIGHT_PIXELS,
    MIN_CLUSTER_PIXELS,
)


# ---------------------------------------------------------------------------
# Image fixtures — synthesize BGR images with specific HSV characteristics
# ---------------------------------------------------------------------------

def _make_solid_hsv(h, s, v, size=(1040, 745)):
    """Make a BGR image with uniform HSV values."""
    hsv = np.zeros((size[0], size[1], 3), dtype=np.uint8)
    hsv[:, :, 0] = h
    hsv[:, :, 1] = s
    hsv[:, :, 2] = v
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def _make_reference_saturated_bright():
    """A colorful reference image with bright saturated pixels — like a
    basic land art. High saturation in bright areas, clustered hue."""
    # Fill with bright saturated orange (h=15, s=200, v=230)
    return _make_solid_hsv(15, 200, 230)


def _make_reference_normal():
    """A typical reference: moderate saturation, moderate brightness,
    clustered hue."""
    img = _make_solid_hsv(60, 120, 150, size=(1040, 745))
    # Add a bright patch in the top portion (~10% of pixels above V=220)
    img_hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    img_hsv[:100, :, 2] = 230  # bright band at top
    img_hsv[:100, :, 1] = 180  # saturated bright band
    img_hsv[:100, :, 0] = 60
    return cv2.cvtColor(img_hsv, cv2.COLOR_HSV2BGR)


def _make_old_lighting_foil_scan(ref_bgr):
    """Synthesize an OLD-LIGHTING foil scan: start from reference, add
    specular hotspots that are brighter, desaturated, and hue-scattered.

    NOTE: under the current LED scanner, real foils read darker overall
    with saturated rainbow hotspots — the opposite of this fixture. Use
    this only to exercise signal-direction logic, not to validate the
    classifier. For classifier regression, use the pinned test below.
    """
    out = ref_bgr.copy()
    hsv = cv2.cvtColor(out, cv2.COLOR_BGR2HSV)
    h, w = hsv.shape[:2]

    # Add 30 randomly-placed bright desaturated rainbow hotspots
    rng = np.random.default_rng(42)
    for _ in range(30):
        cx = rng.integers(20, w - 20)
        cy = rng.integers(20, h - 20)
        radius = rng.integers(10, 25)
        # Each hotspot gets a different hue across the rainbow
        hue = int(rng.integers(0, 180))
        y0, y1 = max(0, cy - radius), min(h, cy + radius)
        x0, x1 = max(0, cx - radius), min(w, cx + radius)
        hsv[y0:y1, x0:x1, 0] = hue       # scattered hue
        hsv[y0:y1, x0:x1, 1] = 30        # low saturation (whitish)
        hsv[y0:y1, x0:x1, 2] = 250       # very bright

    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def _make_nonfoil_scan_from_reference(ref_bgr):
    """A nonfoil scan: essentially the reference with small noise.
    Should score low."""
    out = ref_bgr.copy()
    rng = np.random.default_rng(7)
    noise = rng.integers(-5, 6, out.shape, dtype=np.int16)
    out = np.clip(out.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------------------
# Unit tests: _compute_bright_stats
# ---------------------------------------------------------------------------

class TestComputeBrightStats(unittest.TestCase):

    def test_dark_image_returns_insufficient_bright(self):
        dark = _make_solid_hsv(0, 0, 100)  # uniformly dark
        stats = _compute_bright_stats(dark)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["n_bright"], 0)
        self.assertIsNone(stats["mean_s_bright"])

    def test_bright_saturated_image(self):
        bright = _make_solid_hsv(10, 200, 240)  # all pixels bright
        stats = _compute_bright_stats(bright)
        self.assertIsNotNone(stats)
        self.assertGreater(stats["n_bright"], MIN_BRIGHT_PIXELS)
        # Every pixel is saturated orange
        self.assertAlmostEqual(stats["mean_s_bright"], 200, delta=2)
        # Single hue -> small range
        self.assertLess(stats["hue_range_bright"], 2)

    def test_bright_desaturated_image(self):
        bright = _make_solid_hsv(10, 30, 240)  # all pixels bright, desaturated
        stats = _compute_bright_stats(bright)
        self.assertLess(stats["mean_s_bright"], 50)

    def test_none_input(self):
        self.assertIsNone(_compute_bright_stats(None))

    def test_empty_array(self):
        empty = np.array([], dtype=np.uint8).reshape(0, 0, 3)
        self.assertIsNone(_compute_bright_stats(empty))


# ---------------------------------------------------------------------------
# Unit tests: detect_foil
# ---------------------------------------------------------------------------

class TestDetectFoil(unittest.TestCase):

    def test_synthetic_old_lighting_foil_runs_without_error(self):
        """Old-lighting synthetic foil: we don't assert is_foil because
        the current weights are tuned for new-lighting physics (where
        real foils look darker, not brighter). Just verify the pipeline
        runs and produces a numeric confidence."""
        ref = _make_reference_normal()
        scan = _make_old_lighting_foil_scan(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        self.assertIsInstance(result["confidence"], float)

    def test_simulated_nonfoil_scores_below_threshold(self):
        ref = _make_reference_normal()
        scan = _make_nonfoil_scan_from_reference(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        self.assertLess(result["confidence"], FOIL_CONFIDENCE_THRESHOLD)
        self.assertFalse(result["is_foil"])

    def test_bright_saturated_nonfoil_not_flagged(self):
        """The false-positive case: a card with naturally bright saturated
        art (e.g. basic land). Reference and scan both have saturated
        bright pixels. Should NOT flag as foil."""
        ref = _make_reference_saturated_bright()
        scan = _make_nonfoil_scan_from_reference(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        self.assertFalse(result["is_foil"])

    def test_no_reference_returns_no_reference_reason(self):
        scan = _make_reference_normal()
        result = detect_foil(scan, card_id=None, reference_img=None)
        self.assertEqual(result["reason"], "no_reference")
        self.assertFalse(result["is_foil"])
        self.assertEqual(result["confidence"], 0.0)

    def test_missing_card_id_file_returns_no_reference(self):
        """card_id points at a file that doesn't exist -> no_reference."""
        scan = _make_reference_normal()
        result = detect_foil(scan, card_id="nonexistent-card-id-xyz")
        self.assertEqual(result["reason"], "no_reference")

    def test_dark_scan_returns_insufficient_bright(self):
        scan = _make_solid_hsv(0, 0, 50)  # uniformly very dark
        ref = _make_reference_normal()
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "insufficient_bright")
        self.assertFalse(result["is_foil"])

    def test_none_scan_returns_invalid_image(self):
        result = detect_foil(None, reference_img=_make_reference_normal())
        self.assertEqual(result["reason"], "invalid_image")
        self.assertFalse(result["is_foil"])

    def test_reference_resized_if_mismatched(self):
        """Reference at different resolution should still produce a result."""
        ref = _make_reference_normal()            # 1040x745
        ref_small = cv2.resize(ref, (370, 520))   # half size
        scan = _make_old_lighting_foil_scan(ref)
        result = detect_foil(scan, reference_img=ref_small)
        # Resize path works; no assertion on is_foil (synthetic is old-lighting)
        self.assertEqual(result["reason"], "ok")

    def test_result_contains_expected_signals(self):
        ref = _make_reference_normal()
        scan = _make_old_lighting_foil_scan(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertIn("signals", result)
        s = result["signals"]
        self.assertIn("scan", s)
        self.assertIn("reference", s)
        self.assertIn("delta_bright_frac", s)
        self.assertIn("delta_mean_s", s)
        self.assertIn("delta_hue_range", s)

    def test_custom_threshold_overrides_default(self):
        """Passing a threshold far above the default should always keep
        is_foil=False regardless of signal values."""
        ref = _make_reference_normal()
        scan = _make_old_lighting_foil_scan(ref)
        default = detect_foil(scan, reference_img=ref)
        high = detect_foil(scan, reference_img=ref, threshold=999.0)
        self.assertAlmostEqual(default["confidence"],
                               high["confidence"], places=5)
        self.assertFalse(high["is_foil"])


# ---------------------------------------------------------------------------
# Foil signal direction sanity checks
# ---------------------------------------------------------------------------

class TestSyntheticOldLightingSignalDirection(unittest.TestCase):
    """Verify the OLD-LIGHTING foil synthesizer produces the signal
    directions it's designed to produce. This tests the fixture, not
    the classifier — under new lighting, real foils produce OPPOSITE
    signs on dbf and dms. Kept here so changes to the synthesizer
    can't silently break reasoning about what it represents."""

    def setUp(self):
        self.ref = _make_reference_normal()
        self.foil = _make_old_lighting_foil_scan(self.ref)
        self.result = detect_foil(self.foil, reference_img=self.ref)

    def test_delta_bright_frac_positive(self):
        self.assertGreater(
            self.result["signals"]["delta_bright_frac"], 0,
            "Synthesizer adds bright hotspots -> dbf > 0"
        )

    def test_delta_mean_s_negative(self):
        self.assertLess(
            self.result["signals"]["delta_mean_s"], 0,
            "Synthesizer adds desaturated hotspots -> dms < 0"
        )

    def test_delta_hue_range_positive(self):
        self.assertGreater(
            self.result["signals"]["delta_hue_range"], 0,
            "Synthesizer scatters hue -> dhr > 0"
        )


# ---------------------------------------------------------------------------
# Pinned regression test — guards against silent weight / bias / threshold
# changes. If you retune the model, update these snapshots deliberately.
# ---------------------------------------------------------------------------

class TestScoreFormulaRegression(unittest.TestCase):
    """Pin the 5-feature Level-2 confidence formula (2026-04-23 retune
    on 70 foils + 375 nonfoils). Each fixture row is a realistic
    (dbf, dms, dnc, dss, dle) tuple and the score the formula must
    produce.

    If this test fails, it means weights / bias / threshold changed
    and downstream behavior will differ. Retune via _foil_tune.py and
    update the pinned values here only after confirming the new
    precision / recall on a labeled set."""

    def test_pinned_weights_and_bias(self):
        """Pin each coefficient exactly — any change requires updating
        this test deliberately."""
        self.assertAlmostEqual(W_DELTA_BRIGHT_FRAC,       -20.6193, places=4)
        self.assertAlmostEqual(W_DELTA_MEAN_S,             +0.08017, places=5)
        self.assertAlmostEqual(W_DELTA_N_BRIGHT_CLUSTERS,  -0.01779, places=5)
        self.assertAlmostEqual(W_DELTA_STD_S_BRIGHT,       -0.02388, places=5)
        self.assertAlmostEqual(W_DELTA_LAPLACIAN_ENERGY,   -0.00585, places=5)
        self.assertAlmostEqual(FOIL_BIAS,                  -0.5425,  places=4)
        self.assertAlmostEqual(FOIL_CONFIDENCE_THRESHOLD,  +0.75,    places=4)

    def test_dhr_no_longer_imported(self):
        """Sanity: the dropped W_HUE_RANGE_BRIGHT constant should
        truly be gone from the module (forces anyone importing it to
        notice and update)."""
        self.assertFalse(hasattr(foil_detect, "W_HUE_RANGE_BRIGHT"),
            "delta_hue_range was dropped in the Level-2 retune; "
            "remove the W_HUE_RANGE_BRIGHT constant from foil_detect.")

    def test_snapshot_scores_match_expectation(self):
        """Pinned scores for representative 5-tuple signal inputs.
        If any drifts by more than 0.001, the model has changed
        silently. Inputs span the full confidence range."""
        # (dbf, dms, dnc, dss, dle, expected_confidence, label)
        cases = [
            # Zero deltas -> just the bias
            (0.0,    0.0,    0,   0.0,    0.0,   -0.5425, "zero deltas"),
            # Strong nonfoil: bright scan, smooth bright structure
            (+0.230, -8.0,  +30, -10.0, -150.0, -5.3437, "strong nonfoil"),
            # Typical nonfoil (close to class means)
            (+0.145, +0.9,   +5,  -3.0,  -90.0, -2.9510, "typical nonfoil"),
            # Mid-range foil (Astelli-Reclaimer-style: dms strong, fewer
            # clusters, higher residual sat std). Just above +0.75
            # threshold so fires as foil.
            (-0.20,  -40.0, -65, +20.0,  -75.0, +1.4921, "mid foil"),
            # Strong foil — multiple signals all align
            (-0.10,  +50.0, -80, +25.0,  -50.0, +6.6466, "strong foil"),
            # Old-physics foil (Mountain mom #280) — expected MISS even
            # under the new model: this card's physics still don't match
            # the new-lighting profile (basic-land bright sky failure
            # mode). Pin it so we'd notice if a future retune catches it.
            (+0.13,  -12.0, -29,  +7.0,  -45.0, -3.5730, "old-physics foil"),
        ]
        for dbf, dms, dnc, dss, dle, expected, label in cases:
            score = (FOIL_BIAS
                     + W_DELTA_BRIGHT_FRAC       * dbf
                     + W_DELTA_MEAN_S            * dms
                     + W_DELTA_N_BRIGHT_CLUSTERS * dnc
                     + W_DELTA_STD_S_BRIGHT      * dss
                     + W_DELTA_LAPLACIAN_ENERGY  * dle)
            self.assertAlmostEqual(
                score, expected, delta=0.001,
                msg=f"Confidence drift for {label} "
                    f"({dbf},{dms},{dnc},{dss},{dle}): "
                    f"got {score:.4f}, expected {expected:.4f}"
            )

    def test_threshold_classifies_pinned_cases_correctly(self):
        """At the shipped threshold (+0.75), the strong-foil and mid-foil
        snapshots should fire; the nonfoil and old-physics-foil snapshots
        should not. Documents the precision/recall trade-off in test form."""
        score_strong_foil = +6.6466
        score_mid_foil    = +1.4921
        score_nonfoil     = -2.9510
        score_old_physics = -3.5730
        self.assertGreaterEqual(score_strong_foil, FOIL_CONFIDENCE_THRESHOLD)
        self.assertGreaterEqual(score_mid_foil,    FOIL_CONFIDENCE_THRESHOLD)
        self.assertLess(score_nonfoil,    FOIL_CONFIDENCE_THRESHOLD)
        # Documented false negative — the model still misses old-physics
        # foils. If a future retune picks this up, score_old_physics will
        # rise and this assertion will need to flip. Keep this here as a
        # tracking marker.
        self.assertLess(score_old_physics, FOIL_CONFIDENCE_THRESHOLD)


# ---------------------------------------------------------------------------
# Reference loading + printings_map fallback
# ---------------------------------------------------------------------------

import tempfile


class TestLoadReference(unittest.TestCase):
    """Verify _load_reference handles direct lookup, the printings_map
    fallback (for cards whose PNG was deduped under a representative
    card_id), and the missing case."""

    def setUp(self):
        # Stand up a temporary REFERENCE_DIR with one fake PNG for a
        # known representative id. Patch foil_detect.REFERENCE_DIR to
        # point at it for the duration of this test.
        self.tmpdir = tempfile.mkdtemp()
        self.rep_id = "rep-uuid-1234"
        self.printing_id = "printing-uuid-5678"
        # 745x1040 BGR red image — valid PNG that cv2.imread will load
        red = np.full((1040, 745, 3), 255, dtype=np.uint8)
        red[:, :, 0] = 0
        red[:, :, 1] = 0
        cv2.imwrite(os.path.join(self.tmpdir, f"{self.rep_id}.png"), red)
        self._orig_ref_dir = foil_detect.REFERENCE_DIR
        foil_detect.REFERENCE_DIR = self.tmpdir
        # Reset the cached inverse map and seed it with our fixture
        self._orig_inv = foil_detect._PRINTING_TO_REP
        foil_detect._PRINTING_TO_REP = {
            self.printing_id: self.rep_id,
            self.rep_id: self.rep_id,  # self-mapping
        }

    def tearDown(self):
        foil_detect.REFERENCE_DIR = self._orig_ref_dir
        foil_detect._PRINTING_TO_REP = self._orig_inv
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_direct_lookup_succeeds_when_png_exists(self):
        img = _load_reference(self.rep_id)
        self.assertIsNotNone(img)
        self.assertEqual(img.shape, (1040, 745, 3))

    def test_falls_back_to_representative_via_printings_map(self):
        """A printing whose own {card_id}.png doesn't exist should still
        resolve via the printings_map inverse to the representative PNG."""
        img = _load_reference(self.printing_id)
        self.assertIsNotNone(img,
            "printings_map fallback should have found the rep PNG")
        self.assertEqual(img.shape, (1040, 745, 3))

    def test_returns_none_when_truly_missing(self):
        img = _load_reference("not-in-map-at-all-xyz")
        self.assertIsNone(img)

    def test_returns_none_for_empty_card_id(self):
        self.assertIsNone(_load_reference(None))
        self.assertIsNone(_load_reference(""))

    def test_returns_none_when_rep_png_also_missing(self):
        """Printing maps to a rep_id, but the rep PNG file isn't on
        disk either."""
        foil_detect._PRINTING_TO_REP["orphan-printing"] = "rep-with-no-png"
        self.assertIsNone(_load_reference("orphan-printing"))


class TestBuildPrintingToRep(unittest.TestCase):
    """Verify the loader gracefully handles missing/broken
    printings_map.json without crashing detect_foil."""

    def test_missing_printings_map_yields_empty_dict(self):
        # Reset the cache, point PRINTINGS_MAP_PATH at a nonexistent file
        orig_path = foil_detect.PRINTINGS_MAP_PATH
        orig_inv = foil_detect._PRINTING_TO_REP
        foil_detect.PRINTINGS_MAP_PATH = "/nonexistent/path/printings_map.json"
        foil_detect._PRINTING_TO_REP = None
        try:
            inv = foil_detect._build_printing_to_rep()
            self.assertEqual(inv, {})
        finally:
            foil_detect.PRINTINGS_MAP_PATH = orig_path
            foil_detect._PRINTING_TO_REP = orig_inv

    def test_malformed_json_yields_empty_dict(self):
        with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", delete=False) as f:
            f.write("{not valid json")
            bad_path = f.name
        orig_path = foil_detect.PRINTINGS_MAP_PATH
        orig_inv = foil_detect._PRINTING_TO_REP
        foil_detect.PRINTINGS_MAP_PATH = bad_path
        foil_detect._PRINTING_TO_REP = None
        try:
            inv = foil_detect._build_printing_to_rep()
            self.assertEqual(inv, {})
        finally:
            foil_detect.PRINTINGS_MAP_PATH = orig_path
            foil_detect._PRINTING_TO_REP = orig_inv
            os.unlink(bad_path)


# ---------------------------------------------------------------------------
# Level-2 diagnostic signals (instrumentation, not yet scored)
# ---------------------------------------------------------------------------

def _make_dark_with_bright_spots(n_spots, spot_size=12, sat=200, hue=10,
                                 size=(1040, 745)):
    """Make a dark image with N bright separated rectangular hotspots.
    Spots are arranged on a grid so they don't merge in the morph close.

    Each spot is `spot_size`px square with V=240 and the given hue/sat.
    """
    h, w = size
    hsv = np.zeros((h, w, 3), dtype=np.uint8)
    hsv[:, :, 2] = 50  # dark background, well below BRIGHT_V_THRESH

    # Lay out n_spots on a square-ish grid with generous spacing so the
    # 3x3 morph close in the detector won't bridge them.
    cols = int(np.ceil(np.sqrt(n_spots)))
    rows = int(np.ceil(n_spots / cols))
    spacing_y = h // (rows + 1)
    spacing_x = w // (cols + 1)
    placed = 0
    for ri in range(rows):
        for ci in range(cols):
            if placed >= n_spots:
                break
            cy = (ri + 1) * spacing_y
            cx = (ci + 1) * spacing_x
            y0, y1 = cy - spot_size // 2, cy + spot_size // 2
            x0, x1 = cx - spot_size // 2, cx + spot_size // 2
            hsv[y0:y1, x0:x1, 0] = hue
            hsv[y0:y1, x0:x1, 1] = sat
            hsv[y0:y1, x0:x1, 2] = 240
            placed += 1
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def _make_dark_with_rainbow_spots(n_spots, spot_size=12, size=(1040, 745)):
    """Like _make_dark_with_bright_spots but each spot has a different
    hue + saturation, simulating a foil's rainbow specular hotspots."""
    h, w = size
    hsv = np.zeros((h, w, 3), dtype=np.uint8)
    hsv[:, :, 2] = 50

    cols = int(np.ceil(np.sqrt(n_spots)))
    rows = int(np.ceil(n_spots / cols))
    spacing_y = h // (rows + 1)
    spacing_x = w // (cols + 1)
    rng = np.random.default_rng(123)
    placed = 0
    for ri in range(rows):
        for ci in range(cols):
            if placed >= n_spots:
                break
            cy = (ri + 1) * spacing_y
            cx = (ci + 1) * spacing_x
            y0, y1 = cy - spot_size // 2, cy + spot_size // 2
            x0, x1 = cx - spot_size // 2, cx + spot_size // 2
            hue = int(rng.integers(0, 180))
            sat = int(rng.integers(50, 255))
            hsv[y0:y1, x0:x1, 0] = hue
            hsv[y0:y1, x0:x1, 1] = sat
            hsv[y0:y1, x0:x1, 2] = 240
            placed += 1
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


class TestNewBrightStatsFields(unittest.TestCase):
    """Verify the three Level-2 diagnostic fields show up in
    _compute_bright_stats output and behave as expected on synthetic
    inputs designed to isolate each signal."""

    def test_fields_present_on_bright_image(self):
        bright = _make_solid_hsv(10, 200, 240)
        stats = _compute_bright_stats(bright)
        self.assertIn("n_bright_clusters", stats)
        self.assertIn("std_s_bright", stats)
        self.assertIn("laplacian_energy_bright", stats)
        self.assertIsInstance(stats["n_bright_clusters"], int)
        self.assertIsInstance(stats["std_s_bright"], float)
        self.assertIsInstance(stats["laplacian_energy_bright"], float)

    def test_fields_none_when_insufficient_bright(self):
        dark = _make_solid_hsv(0, 0, 100)
        stats = _compute_bright_stats(dark)
        # All three new fields should also degrade gracefully
        self.assertIsNone(stats["n_bright_clusters"])
        self.assertIsNone(stats["std_s_bright"])
        self.assertIsNone(stats["laplacian_energy_bright"])

    def test_uniform_bright_yields_one_cluster(self):
        """A solid bright fill is one connected component."""
        bright = _make_solid_hsv(10, 200, 240)
        stats = _compute_bright_stats(bright)
        self.assertEqual(stats["n_bright_clusters"], 1)

    def test_many_separated_spots_yield_many_clusters(self):
        """40 spaced bright spots on dark background => ~40 clusters.
        (The morph close shouldn't bridge them given the spacing.)"""
        img = _make_dark_with_bright_spots(40)
        stats = _compute_bright_stats(img)
        # Allow a small slop — some spots near edges may get clipped
        self.assertGreater(stats["n_bright_clusters"], 30,
            f"expected many clusters, got {stats['n_bright_clusters']}")
        self.assertLessEqual(stats["n_bright_clusters"], 40)

    def test_uniform_bright_yields_low_saturation_std(self):
        """All bright pixels share the same saturation -> std ≈ 0."""
        bright = _make_solid_hsv(10, 200, 240)
        stats = _compute_bright_stats(bright)
        self.assertLess(stats["std_s_bright"], 1.0)

    def test_rainbow_spots_yield_high_saturation_std(self):
        """Random hue+sat hotspots -> wide saturation distribution."""
        img = _make_dark_with_rainbow_spots(40)
        stats = _compute_bright_stats(img)
        # Spots span sat 50..255 -> std should be tens
        self.assertGreater(stats["std_s_bright"], 30,
            f"expected wide sat std, got {stats['std_s_bright']:.1f}")

    def test_uniform_bright_yields_low_laplacian_energy(self):
        """Smooth uniform fill has Laplacian ≈ 0 everywhere."""
        bright = _make_solid_hsv(10, 200, 240)
        stats = _compute_bright_stats(bright)
        self.assertLess(stats["laplacian_energy_bright"], 1.0)

    def test_spotty_bright_yields_higher_laplacian_energy(self):
        """Many sharp bright/dark transitions raise mean |Laplacian|
        within the bright mask. The bright pixels live at the spot
        interiors but the Laplacian kernel reaches out to the dark
        ring, so the boundary pixels score high."""
        spotty = _make_dark_with_bright_spots(40)
        smooth = _make_solid_hsv(10, 200, 240)
        spotty_stats = _compute_bright_stats(spotty)
        smooth_stats = _compute_bright_stats(smooth)
        self.assertGreater(spotty_stats["laplacian_energy_bright"],
                           smooth_stats["laplacian_energy_bright"] + 5)


class TestDetectFoilExposesNewDeltas(unittest.TestCase):
    """detect_foil() must surface the three new deltas in result['signals']
    so _foil_tune.py and the review tool can read them."""

    def test_new_deltas_present(self):
        ref = _make_reference_normal()
        scan = _make_old_lighting_foil_scan(ref)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        s = result["signals"]
        self.assertIn("delta_n_bright_clusters", s)
        self.assertIn("delta_std_s_bright", s)
        self.assertIn("delta_laplacian_energy", s)
        for k in ("delta_n_bright_clusters",
                  "delta_std_s_bright",
                  "delta_laplacian_energy"):
            self.assertIsInstance(s[k], float, f"{k} should be float")

    def test_new_deltas_near_zero_when_scan_equals_ref(self):
        """If the scan is essentially the reference, the cluster-count
        and saturation-std deltas should be near zero. Laplacian energy
        is intentionally sensitive to per-pixel noise (it's a high-pass
        filter), so a noise-augmented scan can shift it noticeably even
        without specular content — that's a property of the signal, not
        a bug. We assert it stays bounded but don't pin it tight."""
        ref = _make_reference_normal()
        # nonfoil scan is the ref + small noise
        scan = _make_nonfoil_scan_from_reference(ref)
        result = detect_foil(scan, reference_img=ref)
        s = result["signals"]
        self.assertAlmostEqual(s["delta_n_bright_clusters"], 0, delta=2)
        self.assertAlmostEqual(s["delta_std_s_bright"], 0.0, delta=3.0)
        # Laplacian: just bound it — under ±5 pixel noise we observe
        # ~25 units of delta even for a non-foil. The classifier will
        # have to learn to discount this baseline.
        self.assertLess(abs(s["delta_laplacian_energy"]), 50)

    def test_rainbow_spotty_scan_lifts_cluster_and_std(self):
        """A scan with many rainbow hotspots vs a uniform-bright reference
        should produce positive delta_n_bright_clusters AND positive
        delta_std_s_bright. This is the foil-vs-sky discrimination case."""
        ref = _make_solid_hsv(10, 200, 240)  # uniform bright "sky"
        scan = _make_dark_with_rainbow_spots(40)
        result = detect_foil(scan, reference_img=ref)
        self.assertEqual(result["reason"], "ok")
        s = result["signals"]
        # Rainbow scan has ~40 clusters; ref has 1 -> delta should be >> 0
        self.assertGreater(s["delta_n_bright_clusters"], 25,
            f"got {s['delta_n_bright_clusters']}")
        self.assertGreater(s["delta_std_s_bright"], 20,
            f"got {s['delta_std_s_bright']:.1f}")

    def test_new_deltas_contribute_to_confidence(self):
        """After the Level-2 retune the 3 new deltas ARE in the scoring
        formula. Verify the confidence matches the full 5-feature sum
        (and does NOT include the dropped dhr signal)."""
        ref = _make_reference_normal()
        scan = _make_old_lighting_foil_scan(ref)
        result = detect_foil(scan, reference_img=ref)
        s = result["signals"]
        expected = (FOIL_BIAS
                    + W_DELTA_BRIGHT_FRAC       * s["delta_bright_frac"]
                    + W_DELTA_MEAN_S            * s["delta_mean_s"]
                    + W_DELTA_N_BRIGHT_CLUSTERS * s["delta_n_bright_clusters"]
                    + W_DELTA_STD_S_BRIGHT      * s["delta_std_s_bright"]
                    + W_DELTA_LAPLACIAN_ENERGY  * s["delta_laplacian_energy"])
        self.assertAlmostEqual(result["confidence"], expected, places=4)


# ---------------------------------------------------------------------------
# DFC back-face handling (Pattern 1 FP fix, 2026-04-23)
# ---------------------------------------------------------------------------
class TestDFCBackFaceHandling(unittest.TestCase):
    """Verify that for transform/MDFC cards detect_foil auto-corrects
    when the scanned face differs from the front-face reference PNG.

    Real-world failure this catches: MOM battles identified via the
    front-face hash (horizontal battle art) but actually scanned from
    the back (vertical creature art). Without this fix the scan vs ref
    brightness delta is garbage and mimics a foil signature.
    """

    def setUp(self):
        # Build a temp REFERENCE_DIR containing a front and back PNG
        # for a known card_id. Each face has a distinct bright_frac so
        # the "closer brightness" heuristic has something to pick
        # between.
        self.tmpdir = tempfile.mkdtemp()
        self.card_id = "dfc-card-uuid-4242"

        # Front: a BRIGHT reference (big bright band in top half)
        #   -> bright_frac ~ 0.50
        front = np.zeros((1040, 745, 3), dtype=np.uint8) + 100  # dim gray
        front[:520, :, :] = 250  # top half near white -> V > 220
        cv2.imwrite(
            os.path.join(self.tmpdir, f"{self.card_id}.png"), front)

        # Back: a DARK reference (only a thin bright stripe)
        #   -> bright_frac ~ 0.05
        back = np.zeros((1040, 745, 3), dtype=np.uint8) + 50  # very dark
        back[:50, :, :] = 250  # thin bright strip
        cv2.imwrite(
            os.path.join(self.tmpdir, f"{self.card_id}__back.png"), back)

        # Patch the module's reference dir
        self._orig_ref_dir = foil_detect.REFERENCE_DIR
        foil_detect.REFERENCE_DIR = self.tmpdir
        # Reset printings-map cache (not needed for this test, but keep
        # it clean)
        self._orig_inv = foil_detect._PRINTING_TO_REP
        foil_detect._PRINTING_TO_REP = {}

    def tearDown(self):
        foil_detect.REFERENCE_DIR = self._orig_ref_dir
        foil_detect._PRINTING_TO_REP = self._orig_inv
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_load_reference_back_finds_back_png(self):
        img = _load_reference_back(self.card_id)
        self.assertIsNotNone(img,
            "back-face PNG should be found via direct lookup")
        self.assertEqual(img.shape, (1040, 745, 3))

    def test_load_reference_back_returns_none_when_no_back_png(self):
        img = _load_reference_back("nonexistent-card-id")
        self.assertIsNone(img)

    def test_load_reference_back_returns_none_for_empty_card_id(self):
        self.assertIsNone(_load_reference_back(None))
        self.assertIsNone(_load_reference_back(""))

    def test_bright_scan_matches_front_face(self):
        """A bright scan (bright_frac ~ 0.50) should pick the front
        face (bright_frac ~ 0.50) over the back (bright_frac ~ 0.05)."""
        bright_scan = np.zeros((1040, 745, 3), dtype=np.uint8) + 100
        bright_scan[:520, :, :] = 250
        result = detect_foil(bright_scan, card_id=self.card_id)
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["signals"]["matched_face"], "front")

    def test_dark_scan_matches_back_face(self):
        """A dark scan (bright_frac ~ 0.05) should pick the back face
        (bright_frac ~ 0.05) over the front (bright_frac ~ 0.50). This
        is the MOM battle failure mode — scanner captured the creature
        back but identifier mapped to the front card_id."""
        dark_scan = np.zeros((1040, 745, 3), dtype=np.uint8) + 50
        dark_scan[:50, :, :] = 250
        result = detect_foil(dark_scan, card_id=self.card_id)
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["signals"]["matched_face"], "back",
            "dark scan should match the dark back-face reference, "
            "not the bright front")

    def test_dfc_back_scan_does_not_trigger_false_foil(self):
        """Regression guard for the exact MOM-battle FP pattern.

        Before this fix: scan of back (dark creature) was always
        compared to front (bright battle) -> dbf strongly negative ->
        scored as high-confidence foil.

        After this fix: the back-face reference is picked, scan vs
        matching face has near-zero dbf, confidence stays near 0."""
        dark_scan = np.zeros((1040, 745, 3), dtype=np.uint8) + 50
        dark_scan[:50, :, :] = 250  # matches back-face pattern
        result = detect_foil(dark_scan, card_id=self.card_id)
        self.assertEqual(result["reason"], "ok")
        self.assertLess(
            result["confidence"], FOIL_CONFIDENCE_THRESHOLD,
            f"dark scan of DFC back-face must not score as foil; "
            f"got confidence={result['confidence']:.3f}")
        # Sanity: the scan-vs-back dbf should be near zero
        self.assertLess(
            abs(result["signals"]["delta_bright_frac"]), 0.10,
            "scan vs correctly-picked back-face ref should have "
            "near-zero dbf")

    def test_explicit_reference_img_bypasses_face_picking(self):
        """When the caller passes reference_img explicitly, don't
        second-guess them — use the provided reference as-is."""
        bright_scan = np.zeros((1040, 745, 3), dtype=np.uint8) + 100
        bright_scan[:520, :, :] = 250
        # Pass an arbitrary reference that doesn't match the scan
        some_ref = np.zeros((1040, 745, 3), dtype=np.uint8) + 200
        result = detect_foil(bright_scan,
                             card_id=self.card_id,
                             reference_img=some_ref)
        self.assertEqual(result["reason"], "ok")
        # matched_face remains "front" (the default) because the
        # face-picking branch didn't run
        self.assertEqual(result["signals"]["matched_face"], "front")

    def test_no_back_png_falls_through_to_front_only(self):
        """Cards without a __back.png (layout=normal) should use the
        front reference, unchanged from the prior behavior."""
        # Remove the back PNG for this test
        os.remove(os.path.join(self.tmpdir, f"{self.card_id}__back.png"))
        dark_scan = np.zeros((1040, 745, 3), dtype=np.uint8) + 50
        dark_scan[:50, :, :] = 250
        result = detect_foil(dark_scan, card_id=self.card_id)
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["signals"]["matched_face"], "front",
            "with no back PNG available, matched_face must stay 'front'")


if __name__ == "__main__":
    unittest.main()
