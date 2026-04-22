# card_detect.py
# ---------------------------------------------------------------------------
# Card contour detection + perspective warp.
#
# Per-channel Canny edge detection with multi-channel OR fallback.
# Tries each channel independently first (Gray, B, G, R) — this avoids
# OR-combining channels from merging card edges with the staging platform.
# Falls back to multi-channel OR only when no single channel finds the card
# (some cards need edges from multiple channels to form a complete contour).
#
# Dual morphological close (rect + ellipse kernels OR'd) bridges gaps
# at rounded card corners so the full card border closes into a contour.
#
# Uses the staging ROI (from staging_roi.json) to:
#   - Set a dynamic minimum area (reject text boxes / small features)
#   - Set a maximum area (reject the staging platform itself)
#   - Filter by location (reject contours on bins/motor/etc.)
#
# No background subtraction, no color segmentation.
# ---------------------------------------------------------------------------

import os
import json
import cv2
import numpy as np

# --- Card dimensions (must match Scryfall PNGs / hash DB) ---
CARD_WIDTH = 745
CARD_HEIGHT = 1040
CARD_ASPECT_RATIO = CARD_HEIGHT / CARD_WIDTH  # ~1.396

# --- Staging ROI ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STAGING_ROI_PATH = os.path.join(SCRIPT_DIR, "staging_roi.json")

_staging_roi = None       # shape (4, 2) float32
_staging_roi_area = None  # polygon area in pixels
_staging_roi_loaded = False


def _load_staging_roi():
    """Load the staging ROI from disk (once)."""
    global _staging_roi, _staging_roi_area, _staging_roi_loaded
    if _staging_roi_loaded:
        return
    _staging_roi_loaded = True
    try:
        with open(STAGING_ROI_PATH, 'r') as f:
            data = json.load(f)
        roi = np.array(data, dtype=np.float32)
        if roi.shape == (4, 2):
            _staging_roi = roi
            _staging_roi_area = float(cv2.contourArea(
                roi.reshape(4, 1, 2).astype(np.int32)))
            print(f"[card_detect] Staging ROI loaded "
                  f"(area={_staging_roi_area:.0f}px)")
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as e:
        print(f"[card_detect] No staging ROI: {e}")


def invalidate_staging_roi():
    """Force re-load on next detection (call after ROI is recalibrated)."""
    global _staging_roi_loaded
    _staging_roi_loaded = False


# Load on import
_load_staging_roi()


def detect_card(frame, debug=False):
    """
    Detect a card in the camera frame and return a canonical 745x1040 image.

    :param frame: Camera frame (BGR numpy array)
    :param debug: If True, print debug info
    :return: 745x1040 BGR numpy array, or None if no card found
    """
    contour = _find_card_contour(frame, debug=debug)
    if contour is None:
        return None

    card_img = _perspective_warp(frame, contour)
    return card_img


def _is_inside_roi(cx, cy, margin=100):
    """Check if a point is inside the staging ROI (with margin)."""
    if _staging_roi is None:
        return True  # No ROI loaded — accept everything
    dist = cv2.pointPolygonTest(
        _staging_roi.reshape(4, 1, 2), (float(cx), float(cy)), True)
    return dist >= -margin


def _find_card_contour(frame, debug=False):
    """
    Find the card contour using multi-channel Canny + 2-pass morph cascade.

    Strategy: multi-OR edges at Canny 30/90, then run a 2-pass
    morph-close cascade:

      Pass 1: 3x3 kernel, iter=1 (tight — high-contrast cards whose outline
              is already clean; prevents the morph from bridging the card
              edge to adjacent background clutter).
      Pass 2: 3x3 kernel, iter=3 (stronger close — bridges the ~9px gaps
              on low-contrast dark-on-cardboard cards like Battle layouts
              and black-bordered creatures).

    Canny 30/90 (down from 50/150) catches weaker card borders that the
    old thresholds missed, without introducing false positives on empty
    platform noise (verified on 17 empty-platform frames + 7 previously-
    missed real cards + 144 historically successful raw frames).

    Falls back to per-channel (with the same 2-pass) if multi-OR fails.

    :param frame: BGR camera frame
    :param debug: Print debug info
    :return: 4-point contour (shape (4,1,2)) or None
    """
    # Dynamic area thresholds from staging ROI
    if _staging_roi_area and _staging_roi_area > 0:
        min_area = int(_staging_roi_area * 0.28)
        max_area = int(_staging_roi_area * 0.60)
    else:
        min_area = 80000
        max_area = 600000

    # --- Compute Canny edges for each channel ---
    # 30/90 (lowered from 50/150): catches weak card borders on low-contrast
    # cards (dark-on-cardboard battle cards, black-bordered creatures).
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    b, g, r = cv2.split(frame)

    edges_gray = cv2.Canny(cv2.GaussianBlur(gray, (3, 3), 0), 30, 90)
    edges_b = cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 30, 90)
    edges_g = cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 30, 90)
    edges_r = cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 30, 90)

    # --- Primary: multi-channel OR, 2-pass morph cascade ---
    # OR-combine all color channels for maximum edge coverage. This
    # catches card borders invisible in any single channel (e.g. green
    # border on brown cardboard).
    edges_mc = cv2.bitwise_or(
        edges_b, cv2.bitwise_or(edges_g, edges_r))

    best_approx = None
    best_area = 0
    best_channel = None

    # Pass 1: tight morph (iter=1) — card's own outline, no bridging to clutter
    result = _find_best_card_in_edges(
        edges_mc, min_area, max_area, morph_iter=1, debug=debug)
    if result is not None:
        best_approx, best_area = result
        best_channel = "multi-OR pass1"

    # Pass 2: stronger morph (iter=3) — bridge gaps in fragmented card edges
    if best_approx is None:
        result = _find_best_card_in_edges(
            edges_mc, min_area, max_area, morph_iter=3, debug=debug)
        if result is not None:
            best_approx, best_area = result
            best_channel = "multi-OR pass2"

    # --- Fallback: per-channel with outlier rejection ---
    # If multi-OR failed (edges merged card with platform into one huge
    # contour), try each channel independently. Individual channels keep
    # the card separated from the staging platform edges.
    # Reject outlier-large contours (include platform edges), then pick
    # the largest non-outlier (outer card border, not inner frame).
    if best_approx is None:
        candidates = []
        for ch_name, edges in [("gray", edges_gray), ("blue", edges_b),
                               ("green", edges_g), ("red", edges_r)]:
            # Try pass 1 first, then pass 2
            result = _find_best_card_in_edges(
                edges, min_area, max_area, morph_iter=1, debug=False)
            if result is None:
                result = _find_best_card_in_edges(
                    edges, min_area, max_area, morph_iter=3, debug=False)
            if result is not None:
                approx, area = result
                candidates.append((ch_name, approx, area))

        if candidates:
            areas = [a for _, _, a in candidates]
            median_area = float(np.median(areas))

            for ch_name, approx, area in candidates:
                # Skip outliers >15% above median (platform edge noise)
                if len(candidates) > 1 and area > median_area * 1.15:
                    if debug:
                        print(f"[detect] Skipping {ch_name} outlier: "
                              f"area {area:.0f} >> median {median_area:.0f}")
                    continue
                # Largest non-outlier = outer card border
                if area > best_area:
                    best_area = area
                    best_approx = approx
                    best_channel = ch_name + " (fallback)"

    if debug:
        if best_approx is not None:
            print(f"[detect] Found card contour via {best_channel}, "
                  f"area={best_area:.0f} "
                  f"(min={min_area}, max={max_area})")
        else:
            print(f"[detect] No card contour found "
                  f"(area range [{min_area}, {max_area}])")

    # Refine polygon outward: weak morph usually locks onto the inner
    # frame (high contrast); walk each side outward to snap to the real
    # outer card edge so the warp captures the full card, not just the
    # inner frame.
    if best_approx is not None:
        best_approx = _refine_polygon_outward(
            frame, best_approx, search_band=40, debug=debug)

    return best_approx


def _find_best_card_in_edges(edges, min_area, max_area,
                             morph_iter=1, debug=False):
    """
    Apply dual morphological close to edge map and find the best card contour.

    Uses approxPolyDP to verify contours actually simplify to 4 corners
    (a rectangle/card shape) before accepting them. This rejects irregular
    contours (platform edges, merged blobs) that pass area/ratio filters
    but aren't actually cards.

    Falls back to minAreaRect if no clean 4-point polygon is found but a
    contour passes all other checks — some cards have internal features
    that prevent clean polygon simplification.

    :param edges: Single-channel Canny edge map
    :param min_area: Minimum contour area
    :param max_area: Maximum contour area
    :param morph_iter: Morph-close iterations (callers sweep 1 then 3 —
        iter=1 finds clean outlines without bridging card-to-clutter;
        iter=3 bridges small gaps on low-contrast card edges)
    :param debug: Print rejections
    :return: (approx, area) tuple, or None if no valid card found
    """
    # Dual morphological close: rect + ellipse.
    # Rect bridges straight-edge gaps; ellipse bridges curved corners.
    # 3x3 kernel (down from 5x5): smaller kernel prevents card edges from
    # merging with adjacent background clutter through the morph.  The
    # caller does a 2-pass cascade — pass 1 uses iter=1 (tight; card's own
    # outline), pass 2 uses iter=3 (~9px total reach; bridges fragmented
    # low-contrast card edges without reaching far enough to merge with
    # platform/background).  _refine_polygon_outward then walks the polygon
    # outward to the true outer card edge for the perspective warp.
    k_rect = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    k_ellipse = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    closed_rect = cv2.morphologyEx(
        edges, cv2.MORPH_CLOSE, k_rect, iterations=morph_iter)
    closed_elli = cv2.morphologyEx(
        edges, cv2.MORPH_CLOSE, k_ellipse, iterations=morph_iter)
    closed = cv2.bitwise_or(closed_rect, closed_elli)

    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best_approx = None
    best_area = 0
    best_from_poly = False   # prefer polygon-verified contours

    # Fallback: best contour via minAreaRect (no polygon verification)
    fallback_approx = None
    fallback_area = 0

    for cnt in contours:
        area = cv2.contourArea(cnt)

        # Area filter
        if area < min_area:
            continue
        if area > max_area:
            if debug:
                print(f"[detect] Rejected: area {area:.0f} > max {max_area}")
            continue

        # Location filter: center must be on/near the staging platform
        M = cv2.moments(cnt)
        if M["m00"] > 0:
            cx = M["m10"] / M["m00"]
            cy = M["m01"] / M["m00"]
            if not _is_inside_roi(cx, cy, margin=100):
                if debug:
                    print(f"[detect] Rejected: center ({cx:.0f},{cy:.0f}) "
                          f"outside staging ROI")
                continue

        # Solidity check: card area should be close to convex hull area.
        # Rejects irregular/concave contours (merged edges, C-shapes).
        # 0.65 (down from 0.75): black-bordered cards on cardboard produce
        # slightly fragmented Canny edges that lower solidity below 0.75
        # even when the card is cleanly visible.
        hull = cv2.convexHull(cnt)
        hull_area = cv2.contourArea(hull)
        if hull_area > 0:
            solidity = area / hull_area
            if solidity < 0.65:
                if debug:
                    print(f"[detect] Rejected: solidity {solidity:.2f} < 0.65")
                continue

        # Shape filter: card-like aspect ratio via minAreaRect
        rect = cv2.minAreaRect(cnt)
        rw, rh = rect[1]
        if min(rw, rh) < 1:
            continue
        ratio = max(rw, rh) / min(rw, rh)
        if abs(ratio - CARD_ASPECT_RATIO) / CARD_ASPECT_RATIO > 0.35:
            if debug:
                print(f"[detect] Rejected: ratio {ratio:.2f} "
                      f"vs expected {CARD_ASPECT_RATIO:.2f}")
            continue

        # Rectangularity check: contour area vs bounding box area.
        # A card should fill >80% of its bounding rectangle.
        # Catches irregular contours that happen to have a card-like
        # bounding box but aren't actually rectangular.
        box_area = rw * rh
        if box_area > 0:
            rectangularity = area / box_area
            if rectangularity < 0.70:
                if debug:
                    print(f"[detect] Rejected: rectangularity "
                          f"{rectangularity:.2f} < 0.70")
                continue

        # --- Polygon verification via approxPolyDP ---
        # A card contour should simplify to 4 corners.  Try a range of
        # epsilon values (2-8% of perimeter) to handle rounded corners.
        peri = cv2.arcLength(cnt, True)
        poly_found = False
        for eps_pct in [0.02, 0.04, 0.06, 0.08]:
            poly = cv2.approxPolyDP(cnt, eps_pct * peri, True)
            if len(poly) == 4:
                # Verify the 4-point polygon has card-like area and ratio
                poly_area = cv2.contourArea(poly)
                if poly_area >= min_area * 0.8:
                    if area > best_area:
                        best_area = area
                        best_approx = poly.reshape(4, 1, 2)
                        best_from_poly = True
                    poly_found = True
                    break

        # Track as fallback (via minAreaRect) even if polygon check failed
        if not poly_found and area > fallback_area:
            fallback_area = area
            box = cv2.boxPoints(rect)
            fallback_approx = np.intp(box).reshape(4, 1, 2)

    # Prefer polygon-verified contours; fall back to minAreaRect
    if best_approx is not None:
        return best_approx, best_area
    if fallback_approx is not None:
        return fallback_approx, fallback_area
    return None


def _refine_polygon_outward(frame, polygon, search_band=40, debug=False):
    """
    Refine a 4-point polygon by walking each side outward to snap to the
    outermost card edge in a raw Canny edge map.

    The weak morph (5x5) reliably finds the card's INNER frame as a closed
    contour because the inner frame is high-contrast (dark colored frame
    vs light card face). But the actual OUTER card edge (lower contrast
    against cardboard) is what we want for the perspective warp, so the
    crop captures the full card art/title rather than just the inner frame.

    This refinement walks outward from the inner-frame polygon along each
    side's normal, finding the outermost edge pixel within a search band,
    then fits a line through those outermost points per side to produce a
    refined polygon on the true outer card edge. Each side is refined
    independently, so partial refinement (some sides find the outer edge,
    others don't) degrades gracefully.

    If the refined polygon fails sanity checks (area ratio out of bounds),
    falls back to the original polygon.

    :param frame: Source BGR frame
    :param polygon: 4-point polygon, shape (4,1,2), typically on inner frame
    :param search_band: Max pixels outward to search for outer edge
    :param debug: Print diagnostic info
    :return: Refined 4-point polygon, shape (4,1,2)
    """
    pts = polygon.reshape(4, 2).astype(np.float32)

    # Sort corners by angle from centroid so indexing is cyclic (ccw or cw)
    center = pts.mean(axis=0)
    angles = np.arctan2(pts[:, 1] - center[1], pts[:, 0] - center[0])
    order = np.argsort(angles)
    pts = pts[order]

    # Multi-channel Canny edge map (same thresholds as primary detection: 30/90)
    b, g, r = cv2.split(frame)
    edges = cv2.bitwise_or(
        cv2.Canny(cv2.GaussianBlur(b, (3, 3), 0), 30, 90),
        cv2.bitwise_or(
            cv2.Canny(cv2.GaussianBlur(g, (3, 3), 0), 30, 90),
            cv2.Canny(cv2.GaussianBlur(r, (3, 3), 0), 30, 90),
        ),
    )

    h, w = edges.shape
    num_samples = 40  # Sample points along each side

    refined_lines = []  # (vx, vy, px, py) per side from cv2.fitLine

    for i in range(4):
        p0 = pts[i]
        p1 = pts[(i + 1) % 4]

        side = p1 - p0
        side_len = float(np.linalg.norm(side))
        if side_len < 1:
            return polygon  # Degenerate polygon
        side_unit = side / side_len

        # Outward normal: rotate 90°, flip if it points toward center
        normal = np.array(
            [-side_unit[1], side_unit[0]], dtype=np.float32)
        mid = (p0 + p1) / 2
        if float(np.dot(normal, mid - center)) < 0:
            normal = -normal

        outer_points = []
        # Sample along the side (avoid extreme ends near corners)
        for t in np.linspace(0.1, 0.9, num_samples):
            base = p0 + t * side
            best_offset = 0
            for d in range(1, search_band + 1):
                test = base + d * normal
                tx = int(round(float(test[0])))
                ty = int(round(float(test[1])))
                if tx < 0 or ty < 0 or tx >= w or ty >= h:
                    break
                if edges[ty, tx] > 0:
                    best_offset = d  # Keep scanning; track OUTERMOST hit
            if best_offset > 0:
                outer_points.append(base + best_offset * normal)

        if len(outer_points) < 8:
            # Not enough edge hits to fit a reliable line; keep original
            if debug:
                print(f"[refine] Side {i}: only {len(outer_points)} "
                      f"edge hits, keeping original line")
            refined_lines.append((
                float(side_unit[0]), float(side_unit[1]),
                float(p0[0]), float(p0[1])))
            continue

        outer_arr = np.array(outer_points, dtype=np.float32)
        vx, vy, x0, y0 = cv2.fitLine(
            outer_arr, cv2.DIST_L2, 0, 0.01, 0.01)
        refined_lines.append((
            float(vx[0]), float(vy[0]), float(x0[0]), float(y0[0])))
        if debug:
            print(f"[refine] Side {i}: fit line from {len(outer_points)} "
                  f"outer points")

    # Intersect adjacent refined lines to get refined corners.
    # Corner i is intersection of side (i-1) and side i.
    refined_corners = []
    for i in range(4):
        vx1, vy1, px1, py1 = refined_lines[(i - 1) % 4]
        vx2, vy2, px2, py2 = refined_lines[i]

        # Solve: (px1,py1) + t*(vx1,vy1) = (px2,py2) + s*(vx2,vy2)
        A = np.array([[vx1, -vx2], [vy1, -vy2]], dtype=np.float64)
        det = float(np.linalg.det(A))
        if abs(det) < 1e-6:
            # Parallel lines (shouldn't happen on adjacent card sides)
            refined_corners.append(pts[i])
        else:
            rhs = np.array([px2 - px1, py2 - py1], dtype=np.float64)
            ts = np.linalg.solve(A, rhs)
            t = ts[0]
            corner = np.array(
                [px1 + t * vx1, py1 + t * vy1], dtype=np.float32)
            refined_corners.append(corner)

    refined = np.array(refined_corners, dtype=np.float32)

    # Sanity check: refined should be similar shape, slightly larger.
    # If the refinement went off the rails (e.g. snapped to platform
    # edges), the area ratio will be way off.
    orig_area = cv2.contourArea(pts.astype(np.int32))
    if orig_area < 1:
        return polygon
    refined_area = cv2.contourArea(refined.astype(np.int32))
    ratio = refined_area / orig_area

    if ratio < 0.98 or ratio > 1.30:
        if debug:
            print(f"[refine] Rejected refinement: area ratio {ratio:.3f} "
                  f"(outside [0.98, 1.30])")
        return polygon

    if debug:
        print(f"[refine] Accepted: area ratio {ratio:.3f}")

    return refined.reshape(4, 1, 2).astype(np.float32)


def _perspective_warp(frame, approx):
    """
    Warp the 4-point contour region to a canonical 745x1040 card image.

    Handles cards in any orientation: sorts corners by position, checks
    edge lengths to ensure long edges map to the card's height, and
    applies perspective transform.

    :param frame: Source camera frame (BGR)
    :param approx: 4-point contour, shape (4,1,2)
    :return: 745x1040 BGR numpy array
    """
    pts = approx.reshape(4, 2).astype(np.float32)

    # Sort by Y, then by X within top/bottom pairs
    pts = sorted(pts, key=lambda p: p[1])
    top_two = pts[:2]
    bottom_two = pts[2:]
    top_left, top_right = sorted(top_two, key=lambda p: p[0])
    bottom_left, bottom_right = sorted(bottom_two, key=lambda p: p[0])

    # Check edge lengths — if "top" edge is longer than "left" edge,
    # the card is landscape in the frame. Cyclic-shift corners so the
    # long edges map to height (not width).
    top_len = np.linalg.norm(
        np.array(top_right) - np.array(top_left))
    left_len = np.linalg.norm(
        np.array(bottom_left) - np.array(top_left))

    if top_len > left_len:
        top_left, top_right, bottom_right, bottom_left = (
            bottom_left, top_left, top_right, bottom_right)

    src = np.array(
        [top_left, top_right, bottom_right, bottom_left],
        dtype="float32")

    dst = np.array([
        [0, 0],
        [CARD_WIDTH - 1, 0],
        [CARD_WIDTH - 1, CARD_HEIGHT - 1],
        [0, CARD_HEIGHT - 1],
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(frame, M, (CARD_WIDTH, CARD_HEIGHT))

    # Safety: if somehow still landscape, rotate upright
    h, w = warped.shape[:2]
    if w > h:
        warped = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)

    return warped
