# test_detect_only.py
# ---------------------------------------------------------------------------
# Hardware-free detection test. Opens the camera, shows the live feed with
# contour overlay, and identifies whatever card is under the lens using the
# SAME pipeline the web_worker sort loop uses:
#
#   detect_card_on_staging -> is_card_back -> determine_orientation
#     -> compute_combined_distances (layout-aware)
#
# Great for manually swapping cards under the camera without running the
# machine at all.
#
# Controls:
#   SPACE       identify the current frame once
#   A           toggle auto-identify mode (re-runs every ~0.8s)
#   B           CAPTURE staging background reference — CLEAR the platform
#               first, then press B. Detection switches to background
#               subtraction mode once captured, which is far more robust
#               on non-white / textured platforms.
#   X           clear the staging background reference (back to edge mode)
#   R           re-open the camera (useful if the feed freezes)
#   +/-         change camera device index (0..4)
#   ESC / Q     quit
# ---------------------------------------------------------------------------

import os
import sys
import time
import cv2
import numpy as np

print("Loading card data and hashes (this takes a few seconds)...")

from config import (PHASH_DISTANCE_THRESHOLD, EXCLUDED_SETS,
                    STAGING_BG_REF_PATH, ART_REGION)
from detection import (detect_card_on_staging, determine_orientation,
                       detect_border_type, find_card_contour_no_bg,
                       save_staging_bg, load_staging_bg,
                       _find_card_rect_by_bgsub)
from cards import extract_card_info, CARD_DATA_BY_ID
from hashing import compute_combined_distances, is_card_back


# ---------- Camera ----------

def open_camera(index):
    """Open the camera at `index`. Tries default, then CAP_DSHOW, then CAP_MSMF."""
    for backend, name in ((cv2.CAP_ANY, 'CAP_ANY'),
                          (cv2.CAP_DSHOW, 'CAP_DSHOW'),
                          (cv2.CAP_MSMF, 'CAP_MSMF')):
        cap = cv2.VideoCapture(index, backend)
        if cap is not None and cap.isOpened():
            # Match web_camera settings — MJPG is critical on Windows to
            # avoid 1-5 fps mode.
            cap.set(cv2.CAP_PROP_FOURCC,
                    cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
            cap.set(cv2.CAP_PROP_FPS, 30)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            # Warm-up read
            for _ in range(5):
                cap.read()
            print(f"[cam] opened device {index} via {name}")
            return cap
        if cap is not None:
            cap.release()
    print(f"[cam] FAILED to open device {index}")
    return None


# ---------- Identify pipeline ----------

def identify_frame(frame, verbose=True):
    """
    Run the full detection/identification pipeline on a single frame.
    Returns a dict with: ok, card_name, set_code, dist, layout, border,
    is_back, reason, card_img, contour.
    """
    t0 = time.time()

    # 1. Find the card contour (fixed in detection._find_quad_contour to
    #    prefer the smallest valid card-shaped contour so the staging
    #    platform can't win over the real card).
    card_img = detect_card_on_staging(frame)
    t_contour = time.time() - t0
    if card_img is None:
        return {
            'ok': False, 'reason': 'no_card',
            't_contour': t_contour, 't_total': time.time() - t0,
        }

    # 2. Blank card back?
    back_hit, back_dist = is_card_back(card_img)
    if back_hit:
        return {
            'ok': False, 'reason': 'card_back', 'dist': back_dist,
            't_contour': t_contour, 't_total': time.time() - t0,
            'card_img': card_img,
        }

    # 3. Orient + detect layout.
    t1 = time.time()
    card_img, was_rotated, card_layout = determine_orientation(card_img)
    t_orient = time.time() - t1

    border_type = detect_border_type(card_img)

    # 4. Match.
    t2 = time.time()
    dists = compute_combined_distances(card_img, hash_size=16, layout=card_layout)
    t_hash = time.time() - t2

    # Filter to paper + non-excluded sets.
    allowed = []
    for cid, d in dists:
        cd = CARD_DATA_BY_ID.get(cid, {})
        if 'paper' not in cd.get('games', []):
            continue
        if cd.get('set', '').lower() in EXCLUDED_SETS:
            continue
        allowed.append((cid, d))

    if not allowed:
        return {
            'ok': False, 'reason': 'no_candidates',
            'layout': card_layout, 'rotated': was_rotated,
            'border': border_type, 'card_img': card_img,
            't_contour': t_contour, 't_orient': t_orient,
            't_hash': t_hash, 't_total': time.time() - t0,
        }

    top_id, top_dist = allowed[0]
    card_info = extract_card_info(top_id) if top_dist <= PHASH_DISTANCE_THRESHOLD else None

    top5 = []
    for cid, d in allowed[:5]:
        cd = CARD_DATA_BY_ID.get(cid, {}) or {}
        top5.append((cd.get('name', cid), cd.get('set', '?'), d))

    return {
        'ok': card_info is not None,
        'reason': None if card_info else 'below_threshold',
        'card_name': (card_info or {}).get('Name', 'UNRECOGNIZED'),
        'set_code': (card_info or {}).get('Set', '?'),
        'dist': top_dist,
        'layout': card_layout,
        'rotated': was_rotated,
        'border': border_type,
        'card_img': card_img,
        'top5': top5,
        't_contour': t_contour,
        't_orient': t_orient,
        't_hash': t_hash,
        't_total': time.time() - t0,
    }


# ---------- Overlay ----------

def draw_overlay(frame, result):
    """Draw HUD on the live feed frame."""
    h, w = frame.shape[:2]
    out = frame.copy()

    # Draw detected contour (if any) in green. Prefer bg-subtraction if a
    # staging reference has been captured — that's what detection will
    # actually use, so the overlay should match.
    bg_ref = load_staging_bg()
    contour = None
    if bg_ref is not None:
        contour = _find_card_rect_by_bgsub(frame, bg_ref)
    if contour is None:
        contour = find_card_contour_no_bg(
            frame, min_area=10000, expected_ratio=1.4, ratio_tolerance=0.25)
    if contour is not None:
        cv2.drawContours(out, [contour], -1, (0, 255, 0), 2)

    # Mode indicator (top-left)
    mode_label = "BG-SUB" if bg_ref is not None else "EDGE"
    mode_color = (0, 255, 0) if bg_ref is not None else (0, 200, 255)
    cv2.putText(out, f"MODE: {mode_label}", (10, 25),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, mode_color, 2)

    # Bottom info bar
    bar_h = 90
    cv2.rectangle(out, (0, h - bar_h), (w, h), (0, 0, 0), -1)

    if result is None:
        line1 = ("SPACE=id   A=auto   B=capture-bg   X=clear-bg   "
                 "R=reconnect   +/-=device   ESC=quit")
        cv2.putText(out, line1, (10, h - 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.50, (255, 255, 255), 1)
    else:
        if result.get('ok'):
            color = (0, 255, 0)
            line1 = (f"{result['card_name']}  [{result['set_code'].upper()}]  "
                     f"dist={result['dist']:.1f}  layout={result.get('layout', '?')}")
        elif result.get('reason') == 'card_back':
            color = (0, 255, 255)
            line1 = f"CARD BACK  (dist={result.get('dist', 0):.1f})"
        elif result.get('reason') == 'no_card':
            color = (0, 0, 255)
            line1 = "NO CARD CONTOUR FOUND"
        else:
            color = (0, 165, 255)
            reason = result.get('reason', '?')
            dist = result.get('dist')
            if dist is not None:
                line1 = (f"NO MATCH ({reason})  top_dist={dist:.1f}  "
                         f"thresh={PHASH_DISTANCE_THRESHOLD}")
            else:
                line1 = f"NO MATCH ({reason})"

        t_total = result.get('t_total', 0) * 1000
        t_contour = result.get('t_contour', 0) * 1000
        t_orient = result.get('t_orient', 0) * 1000
        t_hash = result.get('t_hash', 0) * 1000
        line2 = (f"total={t_total:.0f}ms  contour={t_contour:.0f}  "
                 f"orient={t_orient:.0f}  hash={t_hash:.0f}")

        cv2.putText(out, line1, (10, h - 55),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.70, color, 2)
        cv2.putText(out, line2, (10, h - 28),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.50, (200, 200, 200), 1)

        # Top 3 candidates (small)
        if result.get('top5'):
            top_line = "  ".join(
                f"{nm[:18]}[{s}]={d:.0f}"
                for nm, s, d in result['top5'][:3]
            )
            cv2.putText(out, top_line, (10, h - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.40, (180, 180, 180), 1)

    return out


# ---------- Main loop ----------

def main():
    device = 0
    if len(sys.argv) > 1:
        try:
            device = int(sys.argv[1])
        except ValueError:
            pass

    cap = open_camera(device)
    if cap is None:
        print("Could not open a camera. Try passing a device index: "
              "python test_detect_only.py 1")
        return

    window = "test_detect_only"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(window, 900, 600)

    last_result = None
    auto_mode = False
    last_auto_t = 0.0
    auto_interval = 0.8  # seconds between auto identifies

    print()
    print("Controls:")
    print("  SPACE = identify one frame")
    print("  A     = toggle auto mode")
    print("  B     = CAPTURE staging background reference "
          "(clear platform first!)")
    print("  X     = clear staging background reference")
    print("  R     = reconnect camera")
    print("  +/-   = change device index")
    print("  ESC / Q = quit")
    if load_staging_bg() is not None:
        print(f"  [bg]  reference loaded from {STAGING_BG_REF_PATH}")
    else:
        print(f"  [bg]  NO reference yet — edge detection mode. "
              f"Press B with an empty platform to switch to bg-sub mode.")
    print()

    while True:
        ok, frame = cap.read()
        if not ok or frame is None:
            # Show a placeholder frame instead of freezing
            blank = np.zeros((480, 640, 3), dtype=np.uint8)
            cv2.putText(blank, "NO FRAME (press R to reconnect)", (20, 240),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
            cv2.imshow(window, blank)
            key = cv2.waitKey(30) & 0xFF
            if key in (27, ord('q'), ord('Q')):
                break
            if key in (ord('r'), ord('R')):
                cap.release()
                cap = open_camera(device)
                if cap is None:
                    break
            continue

        # Auto-identify periodically
        if auto_mode and (time.time() - last_auto_t) >= auto_interval:
            last_result = identify_frame(frame.copy())
            last_auto_t = time.time()
            if last_result.get('ok'):
                print(f"[auto] {last_result['card_name']} "
                      f"[{last_result['set_code']}] "
                      f"dist={last_result['dist']:.1f} "
                      f"layout={last_result.get('layout', '?')} "
                      f"({last_result['t_total']*1000:.0f}ms)")
            elif last_result.get('reason') == 'no_card':
                pass  # too spammy
            else:
                print(f"[auto] NO MATCH ({last_result.get('reason')})")

        overlay = draw_overlay(frame, last_result)
        # Also show the warped card crop in the corner if available
        if last_result and last_result.get('card_img') is not None:
            ci = last_result['card_img']
            thumb_h = 260
            scale = thumb_h / ci.shape[0]
            thumb = cv2.resize(ci, (int(ci.shape[1] * scale), thumb_h))
            th, tw = thumb.shape[:2]
            oh, ow = overlay.shape[:2]
            if tw < ow and th < oh - 95:
                overlay[10:10+th, ow-tw-10:ow-10] = thumb
                cv2.rectangle(overlay, (ow-tw-10, 10),
                              (ow-10, 10+th), (255, 255, 255), 1)

        cv2.imshow(window, overlay)
        key = cv2.waitKey(1) & 0xFF

        if key in (27, ord('q'), ord('Q')):
            break
        elif key == ord(' '):
            last_result = identify_frame(frame.copy())
            # Save everything we have to debug_crops/ for inspection so we
            # can compare warped captures directly against Scryfall refs.
            if last_result.get('card_img') is not None:
                import os
                debug_dir = "debug_crops"
                os.makedirs(debug_dir, exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S")
                crop_path = os.path.join(debug_dir, f"crop_{ts}.png")
                frame_path = os.path.join(debug_dir, f"frame_{ts}.png")
                cv2.imwrite(crop_path, last_result['card_img'])
                cv2.imwrite(frame_path, frame)
                # Also save the ART_REGION sub-crop that the hasher
                # actually sees, with a side-by-side reference if the top
                # candidate image exists on disk.
                ci = last_result['card_img']
                x1, y1, x2, y2 = ART_REGION
                y2c = min(y2, ci.shape[0])
                x2c = min(x2, ci.shape[1])
                art_crop = ci[y1:y2c, x1:x2c]
                art_path = os.path.join(debug_dir, f"art_{ts}.png")
                cv2.imwrite(art_path, art_crop)
                # Side-by-side comparison against top-1 reference, if we
                # can find its PNG on disk.
                top5 = last_result.get('top5') or []
                if top5:
                    # The test script already filtered candidates, so
                    # top5[0] is the post-filter best match. We don't
                    # have its card_id here; grab from CARD_DATA_BY_ID
                    # by name+set if available.
                    from hashing import HASH_DB as _HDB
                    # Try to find any HASH_DB key whose name matches.
                    nm, set_code, _d = top5[0]
                    best_cid = None
                    for cid, cd in CARD_DATA_BY_ID.items():
                        if (cd.get('name', '') == nm
                                and cd.get('set', '') == set_code):
                            best_cid = cid
                            break
                    ref_path = None
                    if best_cid:
                        p = os.path.join("downloaded_cards",
                                         f"{best_cid}.png")
                        if os.path.exists(p):
                            ref_path = p
                    if ref_path:
                        ref_img = cv2.imread(ref_path)
                        if ref_img is not None:
                            # Stack side-by-side for visual diff
                            h1 = ci.shape[0]
                            h2 = ref_img.shape[0]
                            target_h = max(h1, h2)
                            def _fit(img, h):
                                cur_h = img.shape[0]
                                if cur_h == h:
                                    return img
                                scale = h / cur_h
                                return cv2.resize(
                                    img,
                                    (int(img.shape[1] * scale), h))
                            a = _fit(ci, target_h)
                            b = _fit(ref_img, target_h)
                            gap = np.ones((target_h, 10, 3),
                                          dtype=np.uint8) * 255
                            sbs = np.hstack([a, gap, b])
                            sbs_path = os.path.join(
                                debug_dir, f"sidebyside_{ts}.png")
                            cv2.imwrite(sbs_path, sbs)
                            print(f"[debug] side-by-side vs top-1 "
                                  f"({nm} [{set_code}]) -> {sbs_path}")
                print(f"[debug] saved {crop_path}, {art_path}, {frame_path}")
            if last_result.get('ok'):
                print(f"[space] {last_result['card_name']} "
                      f"[{last_result['set_code']}] "
                      f"dist={last_result['dist']:.1f} "
                      f"layout={last_result.get('layout', '?')} "
                      f"border={last_result.get('border', '?')} "
                      f"rotated={last_result.get('rotated')} "
                      f"({last_result['t_total']*1000:.0f}ms)")
                top5 = last_result.get('top5') or []
                for i, (nm, s, d) in enumerate(top5):
                    print(f"   {i+1}. {nm} [{s}]  dist={d:.2f}")
            else:
                print(f"[space] NO MATCH reason={last_result.get('reason')} "
                      f"({last_result['t_total']*1000:.0f}ms)")
                if last_result.get('top5'):
                    for i, (nm, s, d) in enumerate(last_result['top5'][:5]):
                        print(f"   {i+1}. {nm} [{s}]  dist={d:.2f}")
        elif key in (ord('a'), ord('A')):
            auto_mode = not auto_mode
            print(f"[mode] auto = {auto_mode}")
        elif key in (ord('b'), ord('B')):
            # Capture current frame as the empty-platform reference.
            # Grab a fresh frame (drop any buffered one) so the reference
            # matches what the camera is seeing *right now*.
            for _ in range(3):
                cap.read()
            ok2, fresh = cap.read()
            if ok2 and fresh is not None:
                if save_staging_bg(fresh):
                    print("[bg] CAPTURED empty platform reference. "
                          "Detection is now in BG-SUB mode.")
                    last_result = None
                else:
                    print("[bg] failed to save reference")
            else:
                print("[bg] failed to grab a fresh frame")
        elif key in (ord('x'), ord('X')):
            try:
                if os.path.exists(STAGING_BG_REF_PATH):
                    os.remove(STAGING_BG_REF_PATH)
                    print(f"[bg] deleted {STAGING_BG_REF_PATH} — "
                          "back to EDGE mode")
                else:
                    print("[bg] no reference to delete")
                # Force the in-memory cache to reload on next call
                import detection as _det
                _det._STAGING_BG_CACHE = None
                _det._STAGING_BG_MTIME = None
            except Exception as e:
                print(f"[bg] delete error: {e}")
        elif key in (ord('r'), ord('R')):
            print("[cam] reconnecting...")
            cap.release()
            cap = open_camera(device)
            if cap is None:
                print("[cam] reconnect failed")
                break
        elif key in (ord('+'), ord('=')):
            new_device = (device + 1) % 5
            print(f"[cam] switching to device {new_device}")
            cap.release()
            cap = open_camera(new_device)
            if cap is not None:
                device = new_device
            else:
                cap = open_camera(device)
        elif key in (ord('-'), ord('_')):
            new_device = (device - 1) % 5
            print(f"[cam] switching to device {new_device}")
            cap.release()
            cap = open_camera(new_device)
            if cap is not None:
                device = new_device
            else:
                cap = open_camera(device)

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
