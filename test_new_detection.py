# test_new_detection.py
# ---------------------------------------------------------------------------
# Validation harness for the new detection system.
#
# Tests:
#   1. DB self-test — reference images should match themselves
#   2. Rotation test — 180-rotated references should still match
#   3. Scan test — saved scan crops should identify correctly
#   4. Layout coverage — specific layout types in both orientations
#   5. Card back detection
# ---------------------------------------------------------------------------

import os
import sys
import json
import random
import time
import glob
import cv2
import numpy as np
from PIL import Image

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def test_self_match(n=200):
    """
    Test 1: Pick N random reference images, run identify_card().
    They should match themselves with distance ~0.
    """
    from card_identify import identify_card

    images_dir = os.path.join(SCRIPT_DIR, 'downloaded_cards')
    db_path = os.path.join(SCRIPT_DIR, 'card_hashes_v3.json')

    with open(db_path, 'r', encoding='utf-8') as f:
        db = json.load(f)

    # Get card IDs that have images (exclude _card_back)
    card_ids = [k for k in db.keys()
                if k != '_card_back'
                and os.path.exists(os.path.join(images_dir, f"{k}.png"))]

    sample = random.sample(card_ids, min(n, len(card_ids)))

    print(f"\n{'='*60}")
    print(f"TEST 1: Self-match ({len(sample)} random reference images)")
    print(f"{'='*60}")

    matches = 0
    distances = []
    failures = []
    start = time.time()

    for i, cid in enumerate(sample):
        img_path = os.path.join(images_dir, f"{cid}.png")
        img = cv2.imread(img_path)
        if img is None:
            continue

        # Resize to 745x1040 if needed
        h, w = img.shape[:2]
        if (w, h) != (745, 1040):
            img = cv2.resize(img, (745, 1040))

        match_id, dist, was_rotated, _ = identify_card(img)

        # For back-face images, the match might be the canonical ID
        canonical = db.get(cid, {}).get('canonical_id')
        expected = cid if canonical is None else canonical

        if match_id == cid or (canonical and match_id == canonical):
            matches += 1
            distances.append(dist)
        else:
            failures.append((cid, match_id, dist))

        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{len(sample)}] {matches} matches so far...")

    elapsed = time.time() - start

    print(f"\nResults:")
    print(f"  Matched: {matches}/{len(sample)} "
          f"({matches/len(sample)*100:.1f}%)")
    if distances:
        print(f"  Distance: avg={sum(distances)/len(distances):.2f}, "
              f"max={max(distances):.2f}, min={min(distances):.2f}")
    print(f"  Time: {elapsed:.1f}s ({len(sample)/elapsed:.1f} cards/sec)")

    if failures:
        print(f"\n  FAILURES ({len(failures)}):")
        for cid, match_id, dist in failures[:10]:
            print(f"    {cid} -> matched {match_id} (dist={dist:.2f})")

    return matches, len(sample)


def test_rotation(n=100):
    """
    Test 2: Rotate reference images 180 degrees, verify they still match.
    """
    from card_identify import identify_card

    images_dir = os.path.join(SCRIPT_DIR, 'downloaded_cards')
    db_path = os.path.join(SCRIPT_DIR, 'card_hashes_v3.json')

    with open(db_path, 'r', encoding='utf-8') as f:
        db = json.load(f)

    card_ids = [k for k in db.keys()
                if k != '_card_back'
                and os.path.exists(os.path.join(images_dir, f"{k}.png"))]

    sample = random.sample(card_ids, min(n, len(card_ids)))

    print(f"\n{'='*60}")
    print(f"TEST 2: Rotation test ({len(sample)} images, 180-degree)")
    print(f"{'='*60}")

    matches = 0
    rotated_correct = 0
    failures = []

    for cid in sample:
        img_path = os.path.join(images_dir, f"{cid}.png")
        img = cv2.imread(img_path)
        if img is None:
            continue

        h, w = img.shape[:2]
        if (w, h) != (745, 1040):
            img = cv2.resize(img, (745, 1040))

        # Rotate 180
        rotated_img = cv2.rotate(img, cv2.ROTATE_180)
        match_id, dist, was_rotated, _ = identify_card(rotated_img)

        canonical = db.get(cid, {}).get('canonical_id')
        if match_id == cid or (canonical and match_id == canonical):
            matches += 1
            if was_rotated:
                rotated_correct += 1
        else:
            failures.append((cid, match_id, dist))

    print(f"\nResults:")
    print(f"  Matched: {matches}/{len(sample)} "
          f"({matches/len(sample)*100:.1f}%)")
    print(f"  Correctly detected as rotated: {rotated_correct}/{matches}")

    if failures:
        print(f"\n  FAILURES ({len(failures)}):")
        for cid, match_id, dist in failures[:10]:
            print(f"    {cid} -> matched {match_id} (dist={dist:.2f})")

    return matches, len(sample)


def test_scans():
    """
    Test 3: Run all saved scan crops through identify_card().
    """
    from card_identify import identify_card

    scan_pattern = os.path.join(
        SCRIPT_DIR, 'scan_logs', 'session_*', 'card_crops', 'card_*.jpg')
    scan_files = sorted(glob.glob(scan_pattern))

    if not scan_files:
        print(f"\n{'='*60}")
        print("TEST 3: Scan test — NO SCAN FILES FOUND")
        print(f"{'='*60}")
        return 0, 0

    print(f"\n{'='*60}")
    print(f"TEST 3: Scan test ({len(scan_files)} saved scans)")
    print(f"{'='*60}")

    matches = 0
    no_match = 0
    distances = []

    for scan_path in scan_files:
        img = cv2.imread(scan_path)
        if img is None:
            continue

        h, w = img.shape[:2]
        if (w, h) != (745, 1040):
            img = cv2.resize(img, (745, 1040))

        match_id, dist, was_rotated, all_results = identify_card(img)

        basename = os.path.basename(scan_path)
        session = os.path.basename(
            os.path.dirname(os.path.dirname(scan_path)))

        if match_id is not None:
            matches += 1
            distances.append(dist)
            # Try to get card name for display
            name = match_id[:8]
            try:
                from cards import CARD_DATA_BY_ID
                card_data = CARD_DATA_BY_ID.get(match_id)
                if card_data:
                    name = card_data.get('name', match_id[:8])
                # Check canonical ID for back faces
                elif '__back' in match_id:
                    canon = match_id.replace('__back', '')
                    card_data = CARD_DATA_BY_ID.get(canon)
                    if card_data:
                        name = card_data.get('name', '') + ' (back)'
            except ImportError:
                pass

            rot_str = " [ROT]" if was_rotated else ""
            print(f"  {session}/{basename}: {name} "
                  f"(dist={dist:.2f}){rot_str}")
        else:
            no_match += 1
            # Show top 3 near-misses
            top3 = all_results[:3] if all_results else []
            top3_str = ", ".join(
                f"{r[0][:8]}={r[1]:.1f}" for r in top3)
            print(f"  {session}/{basename}: NO MATCH "
                  f"(best={dist:.2f}) [{top3_str}]")

    total = matches + no_match
    print(f"\nResults:")
    print(f"  Matched: {matches}/{total} "
          f"({matches/total*100:.1f}%)" if total else "  No scans")
    if distances:
        print(f"  Distance: avg={sum(distances)/len(distances):.2f}, "
              f"max={max(distances):.2f}")

    return matches, total


def test_card_back():
    """
    Test 4: Card back detection.
    """
    from card_identify import is_card_back

    ref_path = os.path.join(SCRIPT_DIR, 'card_back_reference.png')

    print(f"\n{'='*60}")
    print("TEST 4: Card back detection")
    print(f"{'='*60}")

    passed = 0
    total = 0

    # Test card back reference
    if os.path.exists(ref_path):
        img = cv2.imread(ref_path)
        if img is not None:
            # Crop to 745x1040 if needed
            h, w = img.shape[:2]
            if h > 1040:
                img = img[:1040, :, :]

            is_back, dist = is_card_back(img)
            total += 1
            if is_back:
                passed += 1
                print(f"  card_back_reference.png: PASS "
                      f"(is_back=True, dist={dist:.2f})")
            else:
                print(f"  card_back_reference.png: FAIL "
                      f"(is_back=False, dist={dist:.2f})")

    # Test a few normal cards (should NOT be card backs)
    images_dir = os.path.join(SCRIPT_DIR, 'downloaded_cards')
    normal_files = os.listdir(images_dir)[:5]
    for fname in normal_files:
        img = cv2.imread(os.path.join(images_dir, fname))
        if img is None:
            continue
        h, w = img.shape[:2]
        if (w, h) != (745, 1040):
            img = cv2.resize(img, (745, 1040))

        is_back, dist = is_card_back(img)
        total += 1
        if not is_back:
            passed += 1
            print(f"  {fname[:20]}: PASS "
                  f"(is_back=False, dist={dist:.2f})")
        else:
            print(f"  {fname[:20]}: FAIL "
                  f"(is_back=True, dist={dist:.2f})")

    print(f"\n  Passed: {passed}/{total}")
    return passed, total


def test_layouts():
    """
    Test 5: Verify specific layout types match in both orientations.
    """
    from card_identify import identify_card

    # Find examples of each layout from scryfall data
    cards_json = os.path.join(
        SCRIPT_DIR, 'default-cards-20260412090748.json')

    if not os.path.exists(cards_json):
        print(f"\n{'='*60}")
        print("TEST 5: Layout coverage — SKIPPED (no scryfall data)")
        print(f"{'='*60}")
        return 0, 0

    print(f"\n{'='*60}")
    print("TEST 5: Layout coverage")
    print(f"{'='*60}")

    with open(cards_json, 'r', encoding='utf-8') as f:
        all_cards = json.load(f)

    images_dir = os.path.join(SCRIPT_DIR, 'downloaded_cards')

    # Find one example per layout type
    targets = {
        'normal': None, 'saga': None, 'class': None,
        'case': None, 'adventure': None,
    }

    for c in all_cards:
        layout = c.get('layout', 'normal')
        if layout in targets and targets[layout] is None:
            cid = c.get('id', '')
            if os.path.exists(os.path.join(images_dir, f"{cid}.png")):
                targets[layout] = (cid, c.get('name', 'Unknown'))

        if all(v is not None for v in targets.values()):
            break

    passed = 0
    total = 0

    for layout, info in targets.items():
        if info is None:
            print(f"  {layout}: SKIPPED (no example found)")
            continue

        cid, name = info
        img_path = os.path.join(images_dir, f"{cid}.png")
        img = cv2.imread(img_path)
        if img is None:
            continue

        h, w = img.shape[:2]
        if (w, h) != (745, 1040):
            img = cv2.resize(img, (745, 1040))

        # Test upright
        match_id, dist, was_rot, _ = identify_card(img)
        total += 1
        up_ok = match_id == cid
        if up_ok:
            passed += 1

        # Test rotated
        rot_img = cv2.rotate(img, cv2.ROTATE_180)
        match_id_r, dist_r, was_rot_r, _ = identify_card(rot_img)
        total += 1
        rot_ok = match_id_r == cid
        if rot_ok:
            passed += 1

        status_up = "PASS" if up_ok else "FAIL"
        status_rot = "PASS" if rot_ok else "FAIL"
        print(f"  {layout} ({name[:30]}):")
        print(f"    Upright: {status_up} (dist={dist:.2f})")
        print(f"    Rotated: {status_rot} (dist={dist_r:.2f}, "
              f"detected_rot={was_rot_r})")

    print(f"\n  Passed: {passed}/{total}")
    return passed, total


def main():
    print("=" * 60)
    print("  NEW DETECTION SYSTEM - VALIDATION")
    print("=" * 60)

    results = {}

    results['self_match'] = test_self_match(200)
    results['rotation'] = test_rotation(100)
    results['scans'] = test_scans()
    results['card_back'] = test_card_back()
    results['layouts'] = test_layouts()

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for name, (passed, total) in results.items():
        pct = passed / total * 100 if total > 0 else 0
        status = "OK" if pct >= 90 else "WARN" if pct >= 70 else "FAIL"
        print(f"  {name:15s}: {passed:4d}/{total:4d} "
              f"({pct:5.1f}%) [{status}]")


if __name__ == "__main__":
    main()
