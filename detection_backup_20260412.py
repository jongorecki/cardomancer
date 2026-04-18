# detection.py
# ---------------------------------------------------------------------------
# Contains detection-related functions:
#   1) find_card_contour_no_bg — primary detection (white background)
#   2) find_card_contour_bg_sub
#   3) get_perspective_corrected_card
#   4) contours_are_similar
#   5) detect_aruco_bounding_box — LEGACY, used by CLI only
#   6) scale_corners — LEGACY, used by CLI only
#   7) setup_bounding_box — LEGACY CLI interactive tool
#   8) save_bounding_box / load_bounding_box — LEGACY CLI only
#   9) color_correct_card — per-channel levels correction using card border
#  10) detect_on_white_background — PRIMARY detection for web server
#
# NOTE: The web server uses ONLY detect_on_white_background() for card
# detection. Bounding box functions (5-8) are retained for backward
# compatibility with CLI scripts (main.py, test_*.py) but are not used
# by the web interface.
# ---------------------------------------------------------------------------

import json
import cv2
import numpy as np

from config import (BOUNDING_BOX_PATH, STAGING_ROI_PATH, STAGING_BG_REF_PATH,
                    CARD_WIDTH, CARD_HEIGHT,
                    ART_REGION, ART_REGION_SAGA, ART_REGION_CLASS)


# ---------------------------------------------------------------------------
# Resolution-aware scaling helpers
# ---------------------------------------------------------------------------
# All hardcoded pixel thresholds and morphological kernel sizes in this module
# were originally calibrated at 1280x720 (the "reference" resolution). When
# the camera runs at a different resolution (e.g., 1920x1080) we need to
# scale these values proportionally or detection breaks — kernels are too
# small to close gaps, area thresholds are too low, Hough line lengths are
# too short, etc.
#
# _res_scale(value, frame) multiplies `value` by the ratio of the frame's
# width to the reference width (1280). For 1920x1080 this is 1.5x.

_REFERENCE_WIDTH = 1280


def _res_scale(value, frame, as_odd=False):
    """
    Scale a pixel-domain value from the reference resolution (1280 wide)
    to the actual frame resolution. Returns an int.

    If as_odd=True, ensures the result is odd (required for some OpenCV
    kernel sizes and block sizes).
    """
    if frame is None:
        return int(value)
    w = frame.shape[1] if len(frame.shape) >= 2 else _REFERENCE_WIDTH
    scaled = int(round(value * w / _REFERENCE_WIDTH))
    if as_odd and scaled % 2 == 0:
        scaled += 1
    return max(1, scaled)


def _res_scale_area(value, frame):
    """
    Scale an area threshold by the SQUARE of the resolution ratio.
    A card that occupies 10000 px² at 1280 wide occupies 22500 px² at 1920.
    """
    if frame is None:
        return int(value)
    w = frame.shape[1] if len(frame.shape) >= 2 else _REFERENCE_WIDTH
    ratio = w / _REFERENCE_WIDTH
    return int(round(value * ratio * ratio))


def _find_quad_contour(edge_img, min_area, expected_ratio, ratio_tolerance,
                       max_area=None, debug=False):
    """
    Find the best 4-point polygon contour in an edge image.
    Accepts contours with 4-8 vertices and refines them to 4 points
    using the minimum area bounding rectangle.

    :param max_area: If set, reject contours LARGER than this. Used when
                     detecting inside a staging ROI to reject the platform
                     outline or ROI boundary contour (which are bigger than
                     the card).
    """
    k = _res_scale(3, edge_img, as_odd=True)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    closed = cv2.morphologyEx(edge_img, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if debug:
        print(f"[DEBUG-noBG] Found {len(contours)} contours")

    best_approx = None
    best_area = 0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area < min_area:
            continue
        if max_area is not None and area > max_area:
            if debug:
                print(f"[DEBUG-noBG] Rejected contour: area {area:.0f} "
                      f"> max {max_area:.0f} (platform/ROI boundary?)")
            continue

        epsilon = 0.02 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)

        # Accept 4-point polygons directly
        # Also accept 5-8 point polygons (rounded corners, slight edge noise)
        # and use minAreaRect to get 4 clean corners
        if len(approx) < 4 or len(approx) > 8:
            if debug:
                print(f"[DEBUG-noBG] Rejected contour: {len(approx)} vertices")
            continue

        # Aspect ratio check
        if expected_ratio is not None:
            _, _, bw, bh = cv2.boundingRect(approx)
            ratio = max(bw, bh) / (min(bw, bh) + 1e-6)
            if abs(ratio - expected_ratio) / expected_ratio > ratio_tolerance:
                if debug:
                    print(f"[DEBUG-noBG] Rejected contour: "
                          f"ratio {ratio:.2f} vs expected {expected_ratio:.2f}")
                continue

        if area > best_area:
            best_area = area
            if len(approx) == 4:
                best_approx = approx
            else:
                # Refine to 4 corners using minAreaRect
                rect = cv2.minAreaRect(cnt)
                box = cv2.boxPoints(rect)
                best_approx = np.intp(box).reshape(4, 1, 2)

    return best_approx


def find_card_contour_no_bg(frame,
                            min_area=10000,
                            expected_ratio=None,
                            ratio_tolerance=0.25,
                            canny_low=50,
                            canny_high=150,
                            max_area=None,
                            debug=False):
    """
    Detects card contour in `frame` without using background subtraction.
    Returns the best approx (4-point polygon) or None if not found.

    Uses a multi-strategy approach:
      1) Canny edge detection (standard)
      2) Adaptive threshold (handles uneven lighting)
      3) Otsu threshold (fallback)

    :param frame: The input frame (BGR)
    :param min_area: Minimum area threshold to consider a contour as the card
    :param expected_ratio: Expected height/width ratio of the card (e.g. 1.4).
                           If provided, contours that don't match within
                           ratio_tolerance are rejected.
    :param ratio_tolerance: How far the contour's aspect ratio can deviate
                            from expected_ratio (fraction, default 0.25 = 25%)
    :param canny_low: Lower hysteresis threshold for Canny (default 50).
                      Edges with gradient in [low, high] are kept only if
                      connected to a gradient >= high.
    :param canny_high: Upper hysteresis threshold for Canny (default 150).
                       This is the minimum gradient that will SEED an edge.
                       Cards with softer outer borders (white card on
                       medium-tone background, gradient ~115) need this
                       LOWERED to ~80 or the outer border is invisible and
                       Canny locks onto the interior black frame instead.
    :param max_area: If set, reject contours LARGER than this. Used when
                     detecting inside a staging ROI to reject the platform
                     outline or ROI boundary contour.
    :param debug: Whether to show debug windows/prints
    :return: The best approx (4-point polygon) or None
    """
    # Scale min_area and kernel sizes to the actual frame resolution
    scaled_min_area = _res_scale_area(min_area, frame)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if debug:
        cv2.imshow("Debug - Gray", gray)

    blur_k = _res_scale(5, frame, as_odd=True)
    blurred = cv2.GaussianBlur(gray, (blur_k, blur_k), 0)

    # --- Strategy 1: Canny edge detection ---
    edges = cv2.Canny(blurred, canny_low, canny_high)
    if debug:
        cv2.imshow("Debug - Edges (Canny)", edges)

    result = _find_quad_contour(edges, scaled_min_area, expected_ratio,
                                ratio_tolerance, max_area=max_area, debug=debug)
    if result is not None:
        return result

    # --- Strategy 2: Adaptive threshold ---
    if debug:
        print("[DEBUG-noBG] Canny failed, trying adaptive threshold...")
    adapt_block = _res_scale(15, frame, as_odd=True)
    adaptive = cv2.adaptiveThreshold(blurred, 255,
                                      cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                      cv2.THRESH_BINARY_INV, adapt_block, 4)
    if debug:
        cv2.imshow("Debug - Adaptive", adaptive)

    result = _find_quad_contour(adaptive, scaled_min_area, expected_ratio,
                                ratio_tolerance, max_area=max_area, debug=debug)
    if result is not None:
        return result

    # --- Strategy 3: Otsu threshold ---
    if debug:
        print("[DEBUG-noBG] Adaptive failed, trying Otsu threshold...")
    _, otsu = cv2.threshold(blurred, 0, 255,
                            cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if debug:
        cv2.imshow("Debug - Otsu", otsu)

    result = _find_quad_contour(otsu, scaled_min_area, expected_ratio,
                                ratio_tolerance, max_area=max_area, debug=debug)
    return result

def find_card_contour_bg_sub(frame,
                             bg_frame,
                             min_area=10000,
                             debug=False):
    """
    Detects card contour in `frame` using background subtraction 
    against a previously captured `bg_frame`.

    :param frame: Current frame (BGR)
    :param bg_frame: Background frame (BGR) of same resolution
    :param min_area: Minimum area to consider a contour
    :param debug: Whether to show debug windows/prints
    :return: The best approx (4-point polygon) or None
    """
    scaled_min_area = _res_scale_area(min_area, frame)

    gray_current = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray_bg = cv2.cvtColor(bg_frame, cv2.COLOR_BGR2GRAY)

    diff = cv2.absdiff(gray_bg, gray_current)
    _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)

    if debug:
        cv2.imshow("Debug - BG Diff", diff)
        cv2.imshow("Debug - BG Thresh", thresh)

    k = _res_scale(3, frame, as_odd=True)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if debug:
        print(f"[DEBUG-bgSub] Found {len(contours)} contours in BG-sub approach.")

    best_approx = None
    best_area = 0
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if debug:
            print(f"[DEBUG-bgSub] Contour area = {area}")

        if area >= scaled_min_area:
            epsilon = 0.02 * cv2.arcLength(cnt, True)
            approx = cv2.approxPolyDP(cnt, epsilon, True)
            if len(approx) == 4:
                if area > best_area:
                    best_area = area
                    best_approx = approx

    return best_approx

def get_perspective_corrected_card(frame,
                                   approx,
                                   width=CARD_WIDTH,
                                   height=CARD_HEIGHT):
    """
    Warps the region of `frame` defined by the 4-point contour `approx`
    into a (width x height) image. Ensures the card's long edge ends up
    as the warp's long (vertical) edge, i.e. portrait orientation,
    regardless of how the card was rotated in the source frame.

    Defaults match Scryfall PNG dimensions (745x1040) so the warp produces
    images aligned with the hash DB's coordinate space.

    CRITICAL: the source points must be ordered such that the card's
    SHORT edges map to the dst's short edges and its LONG edges map to
    the dst's long edges — otherwise the warp stretches/squishes the
    card content and phash matching collapses to the noise floor.
    """
    pts = approx.reshape(4, 2).astype(np.float32)
    # Step 1: naive y-then-x sort. This works when the card is roughly
    # upright in the frame but fails for cards rotated ~45-90° because
    # the "top two by y" become the card's LEFT edge (long side),
    # which then gets mapped to the dst's short top edge.
    pts = sorted(pts, key=lambda x: x[1])
    top_two = pts[:2]
    bottom_two = pts[2:]
    top_left, top_right = sorted(top_two, key=lambda x: x[0])
    bottom_left, bottom_right = sorted(bottom_two, key=lambda x: x[0])

    # Step 2: check edge lengths. If the "top" edge is longer than the
    # "left" edge, the card is landscape-in-frame — our labels are 90°
    # off. Cyclically shift the ordering so the LONG card edges map to
    # the LONG dst edges (height) instead of getting squished into the
    # short dst edges (width).
    top_len = np.linalg.norm(np.array(top_right) - np.array(top_left))
    left_len = np.linalg.norm(np.array(bottom_left) - np.array(top_left))

    if top_len > left_len:
        # Cyclic shift by 1: [TL,TR,BR,BL] -> [TR,BR,BL,TL].
        # After the shift the previous "right edge" (long) becomes the
        # new "top edge", mapped to width, which is wrong. So we shift
        # the OTHER way: rotate the corner labels so the old "left
        # edge" (long) becomes the new "left edge" of the labeled
        # quad, i.e. shift by -1: [TL,TR,BR,BL] -> [BL,TL,TR,BR].
        top_left, top_right, bottom_right, bottom_left = (
            bottom_left, top_left, top_right, bottom_right)

    ordered = np.array([top_left, top_right, bottom_right, bottom_left],
                       dtype="float32")

    dst = np.array([
        [0, 0],
        [width - 1, 0],
        [width - 1, height - 1],
        [0, height - 1]
    ], dtype="float32")

    M = cv2.getPerspectiveTransform(ordered, dst)
    warped = cv2.warpPerspective(frame, M, (width, height))

    # Safety net: if for some reason we still got a landscape result
    # (e.g. source points were pathological), rotate it upright. With
    # the length-check fix above this path should rarely trigger.
    h, w, _ = warped.shape
    if w > h:
        warped = cv2.rotate(warped, cv2.ROTATE_90_CLOCKWISE)

    return warped

def contours_are_similar(c1, c2, tolerance=0.01):
    """
    Compares boundingRect areas of two contours to see if they're "similar enough."
    :param c1: contour 1
    :param c2: contour 2
    :param tolerance: fraction difference allowed
    :return: Boolean
    """
    x1, y1, w1, h1 = cv2.boundingRect(c1)
    x2, y2, w2, h2 = cv2.boundingRect(c2)
    area_diff = abs((w1 * h1) - (w2 * h2)) / float((w1 * h1) + 1)
    return area_diff < tolerance

# ----------------------------------------------------------------------------
# ArUco detection (adjusted for older OpenCV versions)
# ----------------------------------------------------------------------------
try:
    from cv2 import aruco
except ImportError:
    # If the user does not have opencv-contrib or is using older naming:
    # Fallback to legacy if needed. You might need:
    # from cv2 import aruco_legacy as aruco
    raise ImportError("OpenCV ArUco not found. Try installing opencv-contrib-python.")

ARUCO_DICT = aruco.getPredefinedDictionary(aruco.DICT_ARUCO_ORIGINAL)
ARUCO_PARAMS = aruco.DetectorParameters()


def detect_aruco_bounding_box(frame,
                             marker_ids=(0, 1, 2, 3),
                             enforce_aspect_ratio=False,
                             target_ratio=1.4,
                             debug=False):
    """
    Detects the specified ArUco markers (IDs=0,1,2,3) in the given frame.
    Returns (corners_rect, all_corners, all_ids):
        - corners_rect: list of 4 points (x,y) for the final bounding rectangle 
                        if all 4 specified markers are found, else None
        - all_corners, all_ids: from the raw aruco.detectMarkers() call, 
                        so we can draw the outlines in main.py

    The expected physical layout is:
       ID 0: top-left
       ID 1: top-right
       ID 2: bottom-right
       ID 3: bottom-left
    """
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    all_corners, all_ids, _ = aruco.detectMarkers(gray, ARUCO_DICT, parameters=ARUCO_PARAMS)

    if all_ids is None or len(all_corners) == 0:
        return None, None, None

    ids_flat = all_ids.flatten()
    found_ids = set(ids_flat)
    required_ids = set(marker_ids)

    # If not all required IDs are found, we can't form the final rectangle
    if not required_ids.issubset(found_ids):
        return None, all_corners, all_ids

    # Compute the center of each found marker
    marker_centers = {}
    for c, marker_id in zip(all_corners, ids_flat):
        pts = c[0]  # shape (4,2)
        cx = np.mean(pts[:,0])
        cy = np.mean(pts[:,1])
        marker_centers[marker_id] = (cx, cy)

    # Check each required marker ID is in marker_centers
    if any(m not in marker_centers for m in marker_ids):
        return None, all_corners, all_ids

    # Extract them in the known order (0->1->2->3)
    top_left  = marker_centers[0]
    top_right = marker_centers[1]
    bot_right = marker_centers[2]
    bot_left  = marker_centers[3]
    corners_rect = [top_left, top_right, bot_right, bot_left]

    # Optional aspect ratio check
    if enforce_aspect_ratio:
        w = np.hypot(top_right[0]-top_left[0], top_right[1]-top_left[1])
        h = np.hypot(bot_left[0]-top_left[0], bot_left[1]-top_left[1])
        ratio = w / (h+1e-6)
        if debug:
            print(f"[detect_aruco_bounding_box] w={w:.2f}, h={h:.2f}, ratio={ratio:.3f}")
        # Could forcibly adjust corners here if needed

    if debug:
        # Draw debug circles/lines here if you like
        pass

    return corners_rect, all_corners, all_ids

def scale_corners(corners, scale_percent):
    """
    Scale the given corners around their geometric center by scale_percent (0..1).
    corners: list of (x,y)
    scale_percent: float in [0,1] => 1.0 = no change, 0.5 = half-size
    """
    pts = np.array(corners, dtype=np.float32)
    center = np.mean(pts, axis=0)  # (cx, cy)
    scaled_pts = center + scale_percent * (pts - center)
    return scaled_pts.tolist()


# ----------------------------------------------------------------------------
# Click-to-set bounding box
# ----------------------------------------------------------------------------

_click_points = []
_click_frame = None


def _mouse_callback(event, x, y, flags, param):
    """Mouse callback for the bounding box setup window."""
    global _click_points, _click_frame
    if event == cv2.EVENT_LBUTTONDOWN and len(_click_points) < 4:
        _click_points.append((x, y))
        labels = ["Top-Left", "Top-Right", "Bottom-Right", "Bottom-Left"]
        idx = len(_click_points) - 1
        print(f"[setup] {labels[idx]}: ({x}, {y})")

        # Draw the point on the display
        if _click_frame is not None:
            cv2.circle(_click_frame, (x, y), 5, (0, 255, 0), -1)
            cv2.putText(_click_frame, labels[idx], (x + 10, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
            if len(_click_points) > 1:
                cv2.line(_click_frame, _click_points[-2], _click_points[-1],
                         (0, 255, 0), 2)
            if len(_click_points) == 4:
                cv2.line(_click_frame, _click_points[3], _click_points[0],
                         (0, 255, 0), 2)


def setup_bounding_box(cap):
    """
    Open a window showing the webcam feed. User clicks 4 corners
    (TL, TR, BR, BL) to define the card region.

    Returns a numpy array of shape (4, 2) with the corner coordinates,
    or None if the user cancelled (ESC).
    """
    global _click_points, _click_frame
    _click_points = []

    window_name = "Click 4 Corners: TL, TR, BR, BL (ESC=cancel, R=reset)"
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window_name, _mouse_callback)

    print("[setup] Click the 4 corners of the card area in order:")
    print("        1) Top-Left  2) Top-Right  3) Bottom-Right  4) Bottom-Left")
    print("        Press R to reset, ESC to cancel.")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # Rotate to match main.py orientation
        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        # Show the frame with any points drawn so far
        display = frame.copy()
        for i, pt in enumerate(_click_points):
            cv2.circle(display, pt, 5, (0, 255, 0), -1)
            if i > 0:
                cv2.line(display, _click_points[i-1], pt, (0, 255, 0), 2)
            if i == 3:
                cv2.line(display, _click_points[3], _click_points[0], (0, 255, 0), 2)

        remaining = 4 - len(_click_points)
        if remaining > 0:
            labels = ["Top-Left", "Top-Right", "Bottom-Right", "Bottom-Left"]
            cv2.putText(display, f"Click: {labels[4 - remaining]} ({remaining} remaining)",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        else:
            cv2.putText(display, "Press ENTER to confirm, R to reset",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        _click_frame = display
        cv2.imshow(window_name, display)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:  # ESC
            cv2.destroyWindow(window_name)
            return None
        elif key == ord('r'):
            _click_points = []
            print("[setup] Reset. Click 4 corners again.")
        elif key == 13 and len(_click_points) == 4:  # ENTER
            corners = np.array(_click_points, dtype=np.float32)
            cv2.destroyWindow(window_name)
            save_bounding_box(corners)
            print(f"[setup] Bounding box set: {_click_points}")
            return corners

    cv2.destroyWindow(window_name)
    return None


def save_bounding_box(corners):
    """Save bounding box corners to a JSON file for reuse."""
    data = corners.tolist() if isinstance(corners, np.ndarray) else corners
    with open(BOUNDING_BOX_PATH, 'w') as f:
        json.dump(data, f)
    print(f"[setup] Bounding box saved to {BOUNDING_BOX_PATH}")


def load_bounding_box():
    """Load saved bounding box corners. Returns numpy array or None."""
    try:
        with open(BOUNDING_BOX_PATH, 'r') as f:
            data = json.load(f)
        corners = np.array(data, dtype=np.float32)
        if corners.shape == (4, 2):
            print(f"[setup] Loaded saved bounding box from {BOUNDING_BOX_PATH}")
            return corners
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return None


# ----------------------------------------------------------------------------
# Staging area ROI — crop frame to just the staging surface
# ----------------------------------------------------------------------------

_staging_click_points = []
_staging_click_frame = None


def _staging_mouse_callback(event, x, y, flags, param):
    """Mouse callback for staging ROI setup."""
    global _staging_click_points, _staging_click_frame
    if event == cv2.EVENT_LBUTTONDOWN and len(_staging_click_points) < 4:
        _staging_click_points.append((x, y))
        labels = ["Top-Left", "Top-Right", "Bottom-Right", "Bottom-Left"]
        idx = len(_staging_click_points) - 1
        print(f"[staging] {labels[idx]}: ({x}, {y})")

        if _staging_click_frame is not None:
            cv2.circle(_staging_click_frame, (x, y), 5, (0, 0, 255), -1)
            cv2.putText(_staging_click_frame, labels[idx], (x + 10, y - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
            if len(_staging_click_points) > 1:
                cv2.line(_staging_click_frame, _staging_click_points[-2],
                         _staging_click_points[-1], (0, 0, 255), 2)
            if len(_staging_click_points) == 4:
                cv2.line(_staging_click_frame, _staging_click_points[3],
                         _staging_click_points[0], (0, 0, 255), 2)


def setup_staging_roi(cap):
    """
    Open a window showing the webcam feed. User clicks 4 corners of the
    staging surface (TL, TR, BR, BL). The frame will be cropped to this
    region before card detection.

    Returns a numpy array of shape (4, 2) or None if cancelled.
    """
    global _staging_click_points, _staging_click_frame
    _staging_click_points = []

    window_name = "Click 4 Corners of STAGING AREA: TL, TR, BR, BL (ESC=cancel, R=reset)"
    cv2.namedWindow(window_name, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(window_name, _staging_mouse_callback)

    print("[staging] Click the 4 corners of the staging surface in order:")
    print("          1) Top-Left  2) Top-Right  3) Bottom-Right  4) Bottom-Left")
    print("          Press R to reset, ESC to cancel.")

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        display = frame.copy()
        for i, pt in enumerate(_staging_click_points):
            cv2.circle(display, pt, 5, (0, 0, 255), -1)
            if i > 0:
                cv2.line(display, _staging_click_points[i-1], pt, (0, 0, 255), 2)
            if i == 3:
                cv2.line(display, _staging_click_points[3],
                         _staging_click_points[0], (0, 0, 255), 2)

        remaining = 4 - len(_staging_click_points)
        if remaining > 0:
            labels = ["Top-Left", "Top-Right", "Bottom-Right", "Bottom-Left"]
            cv2.putText(display, f"Click: {labels[4 - remaining]} ({remaining} remaining)",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
        else:
            cv2.putText(display, "Press ENTER to confirm, R to reset",
                        (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        _staging_click_frame = display
        cv2.imshow(window_name, display)

        key = cv2.waitKey(1) & 0xFF
        if key == 27:
            cv2.destroyWindow(window_name)
            return None
        elif key == ord('r'):
            _staging_click_points = []
            print("[staging] Reset. Click 4 corners again.")
        elif key == 13 and len(_staging_click_points) == 4:
            corners = np.array(_staging_click_points, dtype=np.float32)
            cv2.destroyWindow(window_name)
            save_staging_roi(corners)
            print(f"[staging] Staging ROI set: {_staging_click_points}")
            return corners

    cv2.destroyWindow(window_name)
    return None


def save_staging_roi(corners):
    """Save staging ROI corners to JSON."""
    data = corners.tolist() if isinstance(corners, np.ndarray) else corners
    with open(STAGING_ROI_PATH, 'w') as f:
        json.dump(data, f)
    print(f"[staging] ROI saved to {STAGING_ROI_PATH}")


def load_staging_roi():
    """Load saved staging ROI corners. Returns numpy array or None."""
    try:
        with open(STAGING_ROI_PATH, 'r') as f:
            data = json.load(f)
        corners = np.array(data, dtype=np.float32)
        if corners.shape == (4, 2):
            print(f"[staging] Loaded staging ROI from {STAGING_ROI_PATH}")
            return corners
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        pass
    return None


def crop_to_staging_roi(frame, roi_corners):
    """
    Crop frame to the staging area defined by 4 corners.
    Uses a bounding rect of the corners to extract the ROI region,
    with pixels outside the polygon masked to white (so contour
    detection ignores anything outside the staging surface).
    """
    mask = np.ones(frame.shape[:2], dtype=np.uint8) * 255
    pts = roi_corners.astype(np.int32).reshape((-1, 1, 2))
    cv2.fillPoly(mask, [pts], 0)

    # White out everything outside the staging area
    result = frame.copy()
    result[mask > 0] = (255, 255, 255)

    # Crop to bounding rect for efficiency
    x, y, w, h = cv2.boundingRect(pts)
    cropped = result[y:y+h, x:x+w]
    return cropped


def deskew_card(card_img, max_angle=7):
    """
    Detect and correct slight rotation in a canonical card image.

    Uses Canny edge detection + Hough line transform to find the dominant
    angle of straight lines (card borders, text baselines, art box edges).
    Rotates to correct if the detected skew is within ±max_angle degrees.

    Returns the deskewed image.
    """
    gray = cv2.cvtColor(card_img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)

    # Detect lines using probabilistic Hough transform.
    # These parameters are for the CANONICAL card image (always 745x1040
    # after perspective warp) — NOT the camera frame. Do NOT apply
    # _res_scale() here: the card image size is fixed regardless of
    # camera resolution, and scaling by 745/1280 = 0.58x would shrink
    # the thresholds to ~58% of their tuned values, accepting spurious
    # short/weak lines that corrupt the median angle estimate.
    min_line = 80
    max_gap = 10
    hough_thresh = 100
    lines = cv2.HoughLinesP(edges, 1, np.pi / 180,
                            threshold=hough_thresh,
                            minLineLength=min_line, maxLineGap=max_gap)

    if lines is None or len(lines) == 0:
        return card_img

    # Collect angles of all detected lines
    angles = []
    for line in lines:
        x1, y1, x2, y2 = line[0]
        dx = x2 - x1
        dy = y2 - y1
        # Only consider near-horizontal or near-vertical lines
        angle = np.degrees(np.arctan2(dy, dx))

        # Near-horizontal lines (card top/bottom edges, text baselines)
        if abs(angle) < max_angle:
            angles.append(angle)
        # Near-vertical lines (card side edges) — measure deviation from 90°
        elif abs(abs(angle) - 90) < max_angle:
            deviation = angle - 90 if angle > 0 else angle + 90
            angles.append(deviation)

    if not angles:
        return card_img

    # Use median angle to be robust against outliers
    median_angle = float(np.median(angles))

    # Only correct if the skew is meaningful (> 0.3°) but not too large
    if abs(median_angle) < 0.3 or abs(median_angle) > max_angle:
        return card_img

    # Rotate around center to correct the skew
    h, w = card_img.shape[:2]
    center = (w // 2, h // 2)
    M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
    deskewed = cv2.warpAffine(card_img, M, (w, h),
                               flags=cv2.INTER_LINEAR,
                               borderMode=cv2.BORDER_REPLICATE)

    print(f"[deskew] Corrected {median_angle:.1f}° skew "
          f"(from {len(angles)} lines)")

    return deskewed


def crop_to_bounding_box(frame, corners, debug=False):
    """
    Perspective-correct the card region defined by the saved bounding box
    corners and deskew it.

    :param frame:   Full camera frame (BGR)
    :param corners: Saved bounding box corners, shape (4, 2)
    :param debug:   Show debug windows
    :return:        Canonical card image (CARD_WIDTH x CARD_HEIGHT), deskewed
    """
    approx = corners.reshape((4, 1, 2)).astype(np.float32)
    card_img = get_perspective_corrected_card(frame, approx,
                                              width=CARD_WIDTH, height=CARD_HEIGHT)

    if debug:
        cv2.imshow("Debug - Warped", card_img)

    card_img = deskew_card(card_img)
    return card_img


# ---------------------------------------------------------------------------
# Staging background reference — for background-subtraction detection
# ---------------------------------------------------------------------------

# In-memory cache of the loaded reference. Avoids disk I/O on every frame.
_STAGING_BG_CACHE = None
_STAGING_BG_MTIME = None


def save_staging_bg(frame):
    """
    Save a reference frame of the empty staging platform. Call this with
    NO card under the camera. The saved image is used by
    detect_on_white_background() as the reference for background
    subtraction.
    """
    global _STAGING_BG_CACHE, _STAGING_BG_MTIME
    import os
    ok = cv2.imwrite(STAGING_BG_REF_PATH, frame)
    if ok:
        _STAGING_BG_CACHE = frame.copy()
        try:
            _STAGING_BG_MTIME = os.path.getmtime(STAGING_BG_REF_PATH)
        except OSError:
            _STAGING_BG_MTIME = None
        print(f"[staging_bg] Saved reference to {STAGING_BG_REF_PATH} "
              f"({frame.shape[1]}x{frame.shape[0]})")
        return True
    print(f"[staging_bg] ERROR: failed to write {STAGING_BG_REF_PATH}")
    return False


def load_staging_bg():
    """
    Load the staging platform reference image. Returns a BGR numpy array
    or None if no reference has been captured yet. Reloads automatically
    if the file has been modified since the last load.
    """
    global _STAGING_BG_CACHE, _STAGING_BG_MTIME
    import os
    if not os.path.exists(STAGING_BG_REF_PATH):
        _STAGING_BG_CACHE = None
        _STAGING_BG_MTIME = None
        return None

    try:
        mtime = os.path.getmtime(STAGING_BG_REF_PATH)
    except OSError:
        mtime = None

    if _STAGING_BG_CACHE is not None and mtime == _STAGING_BG_MTIME:
        return _STAGING_BG_CACHE

    img = cv2.imread(STAGING_BG_REF_PATH)
    if img is None:
        _STAGING_BG_CACHE = None
        _STAGING_BG_MTIME = None
        return None

    _STAGING_BG_CACHE = img
    _STAGING_BG_MTIME = mtime
    print(f"[staging_bg] Loaded reference {img.shape[1]}x{img.shape[0]} "
          f"from {STAGING_BG_REF_PATH}")
    return img


def _make_bgsub_foreground_mask(frame, bg_frame, diff_threshold=25,
                                 debug=False):
    """
    Return a binary mask (uint8, 0/255) of pixels that differ from the
    background reference, dilated out so the full card (including its
    border) is inside the mask. This is used to blank out the platform
    before edge-based card detection.
    """
    if frame.shape != bg_frame.shape:
        bg_frame = cv2.resize(bg_frame, (frame.shape[1], frame.shape[0]))

    g_cur = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    g_bg = cv2.cvtColor(bg_frame, cv2.COLOR_BGR2GRAY)
    blur_k = _res_scale(5, frame, as_odd=True)
    g_cur = cv2.GaussianBlur(g_cur, (blur_k, blur_k), 0)
    g_bg = cv2.GaussianBlur(g_bg, (blur_k, blur_k), 0)

    diff = cv2.absdiff(g_bg, g_cur)
    _, mask = cv2.threshold(diff, diff_threshold, 255, cv2.THRESH_BINARY)

    # Close gaps so the card becomes one solid blob. Kernel size scales
    # with resolution so the operation has the same physical effect.
    close_k = _res_scale(9, frame, as_odd=True)
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (close_k, close_k)))
    # Drop salt-and-pepper noise
    open_k = _res_scale(3, frame, as_odd=True)
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (open_k, open_k)))
    # Dilate generously so the card border (which may thresh just
    # inside the blob since blur softens the transition) is definitely
    # inside the mask. We're masking out the platform, not measuring
    # the card, so being generous here is safe.
    dilate_k = _res_scale(15, frame)
    mask = cv2.dilate(
        mask,
        cv2.getStructuringElement(cv2.MORPH_RECT, (dilate_k, dilate_k)))

    if debug:
        cv2.imshow("Debug - BG diff", diff)
        cv2.imshow("Debug - BG fg mask (dilated)", mask)

    return mask


def _sample_bg_fill_color(bg_frame):
    """
    Sample a representative "platform color" from the bg reference so
    we can paint the masked-out platform with a color that matches the
    user's actual background (e.g. cardboard). Using the real platform
    color instead of a synthetic fill (gray/white) means:
      - The mask boundary becomes invisible (no spurious mask-edge
        gradient for Canny to chase).
      - The fill color never clashes with specific card art colors
        (e.g. Shao Jun's gray tones bleeding into middle-gray fill).

    Returns a BGR tuple as Python ints.
    """
    # Median is more robust than mean against any stray pixels in the
    # reference (e.g. a small object that was accidentally on the
    # platform when B was pressed).
    h, w = bg_frame.shape[:2]
    # Sample the central 80% to avoid vignetting / frame edges
    y0, y1 = int(h * 0.1), int(h * 0.9)
    x0, x1 = int(w * 0.1), int(w * 0.9)
    region = bg_frame[y0:y1, x0:x1].reshape(-1, 3)
    med = np.median(region, axis=0)
    return (int(med[0]), int(med[1]), int(med[2]))


def _find_card_rect_by_bgsub(frame, bg_frame, min_area=10000,
                              diff_threshold=25, debug=False):
    """
    Find the card on the staging platform by:
      1. Computing a foreground mask via background subtraction (the
         card is the only thing that changed between bg and current).
      2. Painting the non-foreground (platform) pixels with the SAMPLED
         platform color from bg_frame, so the mask boundary vanishes
         and only real card edges remain.
      3. Running the proven edge-based card detection on the masked
         frame. Canny picks up the actual card border cleanly — the
         platform has been flattened so it can't compete — and
         approxPolyDP snaps to the real corners. This keeps the
         pre-regression warp precision while still rejecting platform
         contours.

    Returns a 4-point (4,1,2) contour in original-frame coordinates,
    or None if no card is found.
    """
    mask = _make_bgsub_foreground_mask(frame, bg_frame,
                                        diff_threshold=diff_threshold,
                                        debug=debug)

    # --- Restrict mask to the central region of the frame ---
    # The camera is centered over the staging platform, so the card is
    # always near the frame center. However, peripheral areas (source
    # bin, destination bins, equipment) also change between the bg
    # reference and the current frame — e.g., when a card is removed
    # from the source bin, the entire bin face changes appearance.
    # Without this restriction, the bg-sub mask includes those
    # peripheral changes and the "largest contour" can be the source
    # bin instead of the card, producing garbage detections.
    #
    # 15% margin on each side → keeps the central 70% of the frame.
    # This comfortably contains the staging platform (typically
    # ~50% of frame width) while excluding surrounding bins.
    h_frame, w_frame = mask.shape[:2]
    margin_x = int(w_frame * 0.15)
    margin_y = int(w_frame * 0.15)  # use width for both to keep square-ish
    mask[:margin_y, :] = 0
    mask[h_frame - margin_y:, :] = 0
    mask[:, :margin_x] = 0
    mask[:, w_frame - margin_x:] = 0

    if debug:
        cv2.imshow("Debug - BG fg mask (ROI-clipped)", mask)

    # Quick sanity: if the mask has nothing card-sized in it, bail
    # (avoids an expensive edge-detection pass on empty platforms).
    scaled_min_area = _res_scale_area(min_area, frame)
    nonzero = cv2.countNonZero(mask)
    if nonzero < scaled_min_area:
        if debug:
            print(f"[bgsub] fg mask has only {nonzero} px "
                  f"(< {min_area}) — no card")
        return None

    # Paint the platform with the ACTUAL sampled cardboard color from
    # the reference. This keeps black-bordered cards detectable
    # (cardboard is bright enough to gradient against black), keeps
    # white-bordered cards detectable (cardboard is dark enough to
    # gradient against white), and — critically — cards whose art
    # happens to contain tones close to the fill color (e.g. grayish
    # art) no longer blend into the masked-out region. The mask
    # boundary itself is invisible because it transitions from
    # cardboard-color to cardboard-color.
    fill_color = _sample_bg_fill_color(bg_frame)
    frame_masked = frame.copy()
    frame_masked[mask == 0] = fill_color

    if debug:
        cv2.imshow("Debug - BG frame masked", frame_masked)

    # Use LOW Canny thresholds on the masked frame. The default (50,150)
    # is calibrated for raw frames where there may be platform texture
    # noise that we don't want to pick up. On the masked frame the
    # outside-card area is solid fill color, so there's no noise there
    # to worry about.
    #
    # Why low thresholds matter: a white-bordered card on sampled
    # cardboard (fill luminance ~140) produces a card-edge gradient of
    # only ~115. Canny's upper threshold is the MINIMUM gradient that
    # will SEED an edge — if we leave it at 150, the 115-gradient outer
    # border is never seeded, and Canny instead locks onto the 255-
    # gradient interior black frame line (white border → black art
    # frame), returning a contour that clips off the entire white
    # border. That clipped warp then hashes to ~85 distance (correct
    # card, misaligned art region) and misses the threshold.
    #
    # (30, 80) comfortably seeds the 115-gradient outer edge while
    # still ignoring the sub-30 gradients from card art interior
    # texture variations.
    contour = find_card_contour_no_bg(
        frame_masked,
        min_area=min_area,
        expected_ratio=1.4,
        ratio_tolerance=0.25,
        canny_low=30,
        canny_high=80,
        debug=debug)

    if debug:
        if contour is not None:
            print("[bgsub] found card via masked edge detection")
        else:
            print("[bgsub] edge detection on masked frame found no card")
    return contour


def detect_on_white_background(frame, debug=False):
    """
    Detect a card on the staging platform. Returns a canonical
    745x1040 card image (matching Scryfall PNG dimensions), or None
    if no card found.

    Detection strategy (in priority order):

      1) **Staging ROI** (preferred): If the user has defined a staging
         ROI (4 corners of the platform), mask the frame to that region
         and run edge-based contour detection inside it. This is fast
         (no background subtraction needed) and robust — the ROI
         excludes bins, equipment, and other clutter, so the biggest
         card-shaped contour inside the ROI is always the card.

      2) **Fallback — full-frame edge detection**: If no ROI is set,
         run contour detection on the entire frame. Works when the card
         has good contrast against the background.

    :param frame: Full camera frame (BGR) showing card on staging surface
    :param debug: Show debug windows
    :return: Canonical card image (CARD_WIDTH x CARD_HEIGHT), deskewed, or None
    """
    contour = None

    # --- Strategy 1: Staging ROI ---
    roi = load_staging_roi()
    if roi is not None:
        # Mask everything outside the ROI polygon to a uniform color
        # so edge detection only finds edges INSIDE the staging area.
        # Use the median color of the ROI CORNER patches as fill so
        # the mask boundary itself doesn't create spurious edges.
        # We sample from corners (not the entire ROI) because the card
        # sits in the middle and would contaminate a full-ROI median.
        roi_int = roi.astype(np.int32).reshape((-1, 1, 2))
        mask = np.zeros(frame.shape[:2], dtype=np.uint8)
        cv2.fillPoly(mask, [roi_int], 255)

        # Calculate ROI area for max_area constraint.
        # The card should be smaller than the ROI — reject any contour
        # that is >= 70% of the ROI area (it's the platform outline).
        roi_area = cv2.contourArea(roi_int)
        max_area = roi_area * 0.7

        # Sample fill color from small patches at each ROI corner.
        # Each patch is a 30x30 region centered on the corner point,
        # clipped to frame bounds. These corners show the bare platform
        # surface (the card is always in the interior, not the corners).
        h, w = frame.shape[:2]
        patch_r = 15  # half-width of the sampling patch
        corner_pixels = []
        for pt in roi.reshape(-1, 2):
            cx, cy = int(pt[0]), int(pt[1])
            y1 = max(0, cy - patch_r)
            y2 = min(h, cy + patch_r)
            x1 = max(0, cx - patch_r)
            x2 = min(w, cx + patch_r)
            if y2 > y1 and x2 > x1:
                corner_pixels.append(frame[y1:y2, x1:x2].reshape(-1, 3))
        if corner_pixels:
            all_corner_px = np.concatenate(corner_pixels, axis=0)
            fill = np.median(all_corner_px, axis=0).astype(np.uint8)
            fill_color = tuple(int(c) for c in fill)
        else:
            fill_color = (180, 180, 180)

        roi_frame = frame.copy()
        roi_frame[mask == 0] = fill_color

        if debug:
            print(f"[detect_white] ROI area={roi_area:.0f}, "
                  f"max_area={max_area:.0f}, fill_color={fill_color}")
            cv2.imshow("Debug - ROI masked frame", roi_frame)

        contour = find_card_contour_no_bg(
            roi_frame,
            min_area=10000,
            expected_ratio=1.4,
            ratio_tolerance=0.25,
            max_area=max_area,
            debug=debug)

        if contour is not None:
            if debug:
                print("[detect_white] Found card via ROI detection")
        elif debug:
            print("[detect_white] ROI detection found no card, "
                  "trying full-frame fallback")

    # --- Strategy 2: Full-frame edge detection (no ROI) ---
    if contour is None:
        contour = find_card_contour_no_bg(frame,
                                           min_area=10000,
                                           expected_ratio=1.4,
                                           ratio_tolerance=0.25,
                                           debug=debug)

    if contour is None:
        if debug:
            print("[detect_white] No card contour found")
        return None

    # Warp from the ORIGINAL frame (not the ROI-masked one) so we
    # get clean card pixels without any fill-color artifacts at edges.
    card_img = get_perspective_corrected_card(frame, contour,
                                              width=CARD_WIDTH,
                                              height=CARD_HEIGHT)
    if debug:
        cv2.imshow("Debug - White BG Warped", card_img)

    card_img = deskew_card(card_img)
    return card_img


def detect_card_layout(card_img):
    """
    Detect the rough layout type of a card image by comparing HSV saturation
    across a three-strip profile (left / center / right thirds).

    Layouts and expected signatures (empirically measured from ~180 real
    cards per bucket — see test_layout_detection.py three-strip stats):

        normal  : L ~ C ~ R     (~0.33 each)            — art fills the card
        saga    : R >> C > L    (R p50 ~0.49)           — art on right
        class   : L >> C > R    (L p50 ~0.52, R ~0.17)  — art in left third
        case    : same as class                         — art in left third
        battle  : L ~ C >> R    (L p50 ~0.42, C ~0.44,  — art in left 2/3
                                 R ~0.14)

    The critical distinction between "class" and "battle" is that battle art
    spans into the center strip: `cf ≈ lf` for battles, but `cf ≈ 0.6 * lf`
    for classes. We use this to separate them.

    Returns one of: "normal", "saga", "class", "battle".

    Cases are folded into "class" since they use the same art region.

    :param card_img: Canonical card image (745x1043 BGR numpy array)
    :return: "normal" | "saga" | "class" | "battle"
    """
    h, w = card_img.shape[:2]

    # Use the middle portion of the card (skip title bar and bottom info)
    y_start = int(h * 0.10)
    y_end = int(h * 0.75)
    middle = card_img[y_start:y_end, :, :]

    hsv = cv2.cvtColor(middle, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1].astype(float)

    third = w // 3
    l_mean = float(np.mean(sat[:, :third]))
    c_mean = float(np.mean(sat[:, third:2 * third]))
    r_mean = float(np.mean(sat[:, 2 * third:]))

    total = l_mean + c_mean + r_mean
    if total == 0:
        return "normal"

    lf = l_mean / total
    cf = c_mean / total
    rf = r_mean / total

    # --- Saga: right strip dominant ---
    # Measured saga rf_p10=0.389, rf_p50=0.485, rf_p90=0.551.
    # Normal rf_p90=0.382 but tail extends to ~0.415 for art-heavy-right
    # normals (Forest, Seachrome Coast). Using 0.42 cleanly separates the
    # two distributions at the cost of ~15% of weak-signal sagas (those
    # with rf in [0.38, 0.42]) falling through to "normal".
    if rf >= 0.42:
        return "saga"

    # --- Left-art layouts (class / case / battle) ---
    # Cutoff: right strip is very low and the left 2 strips dominate.
    # Measured:
    #   class lf_p10=0.463, rf_p90=0.250
    #   battle lf_p10=0.381, rf_p90=0.201
    # We use rf<0.28 (safely below normal rf_p10=0.302) and accept either
    # "left strip dominates" (lf > 0.38) OR "left two strips average high"
    # ((lf+cf)/2 > 0.38). The OR form catches battles like Invasion of
    # Karsus / Shandalar / Eldraine whose lf is just under 0.38 because
    # the art is right-shifted within the left 2/3.
    if rf < 0.28 and (lf > 0.38 or (lf + cf) / 2.0 > 0.38):
        # Battle vs class: battle's art spans both L and C strips, so
        # cf ≈ lf (or even cf > lf). Class's text fills C+R, so cf ≈ 0.6*lf.
        #
        # Measured medians:
        #   class:  cf/lf = 0.312 / 0.516 = 0.605
        #   case:   cf/lf = 0.340 / 0.532 = 0.639
        #   battle: cf/lf = 0.436 / 0.420 = 1.038
        #
        # Threshold at 0.82: catches battles whose cf >= 0.82*lf while
        # leaving class/case below. Very left-heavy battles (Invasion of
        # Fiora, Dragon Brothers) fall into "class" here and are recovered
        # by compute_combined_distances()'s class/battle multi-crop path.
        if lf > 0 and (cf / lf) >= 0.82:
            return "battle"
        return "class"

    # --- Normal: balanced, or anything we can't confidently classify ---
    return "normal"


def color_correct_card(card_img, clip_limit=2.0, grid_size=8, debug=False):
    """
    Normalize card image using CLAHE (Contrast Limited Adaptive Histogram
    Equalization) applied per-channel in LAB color space.

    Unlike global levels correction (which phash is invariant to), CLAHE
    normalizes LOCAL contrast in a grid of tiles across the image. This
    changes the spatial frequency content that phash actually measures,
    making camera-captured images more similar to the clean Scryfall
    reference images used to build the hash database.

    We work in LAB color space so that:
    - L channel gets CLAHE (normalizes luminance contrast)
    - A and B channels get CLAHE (normalizes color contrast)
    This handles both brightness variation and color casts from lighting.

    :param card_img:    Perspective-corrected card image (BGR)
    :param clip_limit:  CLAHE contrast clip limit (higher = more correction)
    :param grid_size:   CLAHE tile grid size (smaller = more local)
    :param debug:       Print info about the correction
    :return:            Corrected card image (BGR)
    """
    # Convert to LAB color space
    lab = cv2.cvtColor(card_img, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)

    # Create CLAHE object
    clahe = cv2.createCLAHE(clipLimit=clip_limit,
                             tileGridSize=(grid_size, grid_size))

    # Apply CLAHE to the L (lightness) channel
    l_corrected = clahe.apply(l_ch)

    if debug:
        l_mean_before = np.mean(l_ch)
        l_std_before = np.std(l_ch)
        l_mean_after = np.mean(l_corrected)
        l_std_after = np.std(l_corrected)
        print(f"[color_correct] L channel: "
              f"mean {l_mean_before:.1f}→{l_mean_after:.1f}, "
              f"std {l_std_before:.1f}→{l_std_after:.1f}")

    # Merge and convert back to BGR
    corrected_lab = cv2.merge([l_corrected, a_ch, b_ch])
    corrected = cv2.cvtColor(corrected_lab, cv2.COLOR_LAB2BGR)

    return corrected


def detect_border_type(card_img, edge_width=15, sat_threshold=50, var_threshold=800):
    """
    Detect whether a card is borderless (full-art) or has a regular border.

    Regular bordered cards have a uniform-color frame along the outer edges
    (black, white, or the frame color). Borderless cards have artwork
    extending to the very edge, producing higher color variance and
    saturation in the edge strips.

    Checks all four edge strips and uses the average saturation and color
    variance to classify.

    :param card_img: Canonical card image (745x1043 BGR numpy array)
    :param edge_width: Width of edge strip to sample in pixels (default 15)
    :param sat_threshold: Mean saturation above this = likely art (default 50)
    :param var_threshold: Color variance above this = likely art (default 800)
    :return: "borderless" or "bordered"
    """
    h, w = card_img.shape[:2]
    hsv = cv2.cvtColor(card_img, cv2.COLOR_BGR2HSV)

    # Sample four edge strips (avoid corners where perspective correction
    # can introduce artifacts — skip 5% from each corner)
    margin = int(min(h, w) * 0.05)

    strips = [
        # Top edge
        hsv[0:edge_width, margin:w - margin],
        # Bottom edge
        hsv[h - edge_width:h, margin:w - margin],
        # Left edge
        hsv[margin:h - margin, 0:edge_width],
        # Right edge
        hsv[margin:h - margin, w - edge_width:w],
    ]

    sat_means = []
    var_scores = []

    for strip in strips:
        # Saturation channel
        sat = strip[:, :, 1].astype(float)
        sat_means.append(np.mean(sat))

        # Color variance across BGR channels
        bgr_strip = card_img[0:edge_width, margin:w - margin] if strip is strips[0] else strip
        # Use variance of all pixel values as a measure of visual complexity
        var_scores.append(np.var(strip.astype(float)))

    avg_sat = np.mean(sat_means)
    avg_var = np.mean(var_scores)

    # Borderless: edges have high saturation (art colors) and high variance
    # Bordered: edges have low saturation (black/white/gold frame) and low variance
    if avg_sat > sat_threshold or avg_var > var_threshold:
        return "borderless"
    return "bordered"


def determine_orientation(card_img, crop_size=745, hash_size=16):
    """
    Determine if the card image is upright or upside-down by comparing
    the full card image in both orientations against the hash database.
    Whichever orientation has a closer match is correct.

    Layout-aware: detects card layout (saga/class/normal) for each
    orientation and uses the correct art region crop for hashing.
    A 180-degree rotation swaps left/right, so a saga upright looks
    like a class when rotated and vice versa.

    Returns (oriented_img, was_rotated, layout, distances).

    The distances list is the full sorted (card_id, distance) result
    from the winning orientation's search. Callers can reuse this
    directly for identification instead of running a redundant third
    search — the orientation step already searched the entire hash DB
    for both orientations.
    """
    from hashing import compute_combined_distances

    rotated = cv2.rotate(card_img, cv2.ROTATE_180)

    upright_layout = detect_card_layout(card_img)
    rotated_layout = detect_card_layout(rotated)

    upright_dists = compute_combined_distances(card_img, hash_size, layout=upright_layout)
    rotated_dists = compute_combined_distances(rotated, hash_size, layout=rotated_layout)

    u_id, u_dist = upright_dists[0] if upright_dists else (None, 999)
    r_id, r_dist = rotated_dists[0] if rotated_dists else (None, 999)

    print(f"[orientation] upright ({upright_layout}): dist={u_dist:.2f}")
    print(f"[orientation] rotated ({rotated_layout}): dist={r_dist:.2f}")

    if r_dist < u_dist:
        return rotated, True, rotated_layout, rotated_dists
    return card_img, False, upright_layout, upright_dists
