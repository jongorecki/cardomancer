# test_hash_selftest.py
# ---------------------------------------------------------------------------
# Hash pipeline self-test. Loads a handful of reference card images directly
# from `downloaded_cards/` and runs them through the SAME hash pipeline the
# web_worker detection path uses. Each reference image should find itself
# in the hash database with distance ~0 (or very close).
#
# If this test passes:
#   -> Hashing is fine; bug is in capture/warp/crop side.
#
# If this test fails (distances >> 0 even for the exact reference image):
#   -> The runtime hash pipeline diverged from the build pipeline. Either
#      the hash DB is stale, or CLAHE/crop/channel order is different, or
#      the DB was rebuilt with a different config than hashing.py expects.
# ---------------------------------------------------------------------------

import os
import sys
import random
import cv2

print("Loading hash DB and card data...")

from config import PHASH_DISTANCE_THRESHOLD, ART_REGION
from hashing import (HASH_DB, PRECOMPUTED_HASHES_BY_LAYOUT,
                     compute_combined_distances)
from cards import CARD_DATA_BY_ID

IMAGES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "downloaded_cards")


def pick_test_cards(n=5, layout_filter="normal"):
    """Pick N random file_keys from the hash DB that we can find on disk."""
    keys = list(HASH_DB.keys())
    random.shuffle(keys)

    picked = []
    for k in keys:
        # Skip back-faces
        if k.endswith("__back"):
            continue
        h = HASH_DB[k]
        # Use layout-tagged cards based on the filter
        if layout_filter is not None:
            lay = h.get("layout", "normal")
            if lay == "case":
                lay = "class"
            if lay != layout_filter:
                continue
        ref_path = os.path.join(IMAGES_DIR, f"{k}.png")
        if not os.path.exists(ref_path):
            continue
        picked.append(k)
        if len(picked) >= n:
            break
    return picked


def run_self_test(card_id, layout="normal"):
    """Load the reference PNG for `card_id` and see if it finds itself."""
    ref_path = os.path.join(IMAGES_DIR, f"{card_id}.png")
    if not os.path.exists(ref_path):
        print(f"  MISSING: {ref_path}")
        return None

    # Load the reference exactly as compute_combined_distances expects: a
    # BGR numpy array of shape (1043, 745, 3).
    img_bgr = cv2.imread(ref_path)
    if img_bgr is None:
        print(f"  FAILED to read {ref_path}")
        return None

    h, w = img_bgr.shape[:2]
    if (w, h) != (745, 1040):
        print(f"  WARNING: unexpected size {w}x{h} (expected 745x1040)")

    # Run the runtime hash path
    results = compute_combined_distances(img_bgr, hash_size=16, layout=layout)
    if not results:
        print("  no results")
        return None

    top_id, top_dist = results[0]
    cd = CARD_DATA_BY_ID.get(top_id, {}) or {}
    name = cd.get('name', top_id)
    set_code = cd.get('set', '?')

    # Find where the "expected" card ranks
    expected_rank = None
    expected_dist = None
    for i, (cid, d) in enumerate(results):
        if cid == card_id:
            expected_rank = i
            expected_dist = d
            break

    # Build context
    exp_cd = CARD_DATA_BY_ID.get(card_id, {}) or {}
    exp_name = exp_cd.get('name', card_id)
    exp_set = exp_cd.get('set', '?')

    return {
        'card_id': card_id,
        'expected_name': exp_name,
        'expected_set': exp_set,
        'top_id': top_id,
        'top_name': name,
        'top_set': set_code,
        'top_dist': top_dist,
        'expected_rank': expected_rank,
        'expected_dist': expected_dist,
    }


def main():
    print()
    print(f"Hash DB: {len(HASH_DB)} entries")
    print(f"  normal bucket:  {len(PRECOMPUTED_HASHES_BY_LAYOUT['normal'])}")
    print(f"  saga bucket:    {len(PRECOMPUTED_HASHES_BY_LAYOUT['saga'])}")
    print(f"  class bucket:   {len(PRECOMPUTED_HASHES_BY_LAYOUT['class'])}")
    print(f"  battle bucket:  {len(PRECOMPUTED_HASHES_BY_LAYOUT['battle'])}")
    print(f"ART_REGION: {ART_REGION}")
    print(f"PHASH_DISTANCE_THRESHOLD: {PHASH_DISTANCE_THRESHOLD}")
    print()

    n_per_bucket = 5
    all_ok = True

    for bucket in ("normal", "saga", "class", "battle"):
        cards = pick_test_cards(n=n_per_bucket, layout_filter=bucket)
        if not cards:
            print(f"[{bucket}] no test cards available on disk — skipping")
            continue

        print(f"[{bucket}] testing {len(cards)} cards")
        for cid in cards:
            result = run_self_test(cid, layout=bucket)
            if result is None:
                all_ok = False
                continue

            is_self = result['top_id'] == cid
            marker = " OK " if (is_self and result['top_dist'] < 5) else "FAIL"
            if not is_self or result['top_dist'] >= 5:
                all_ok = False

            print(f"  [{marker}] {result['expected_name'][:30]:30s} "
                  f"[{result['expected_set']:4s}]  "
                  f"-> top: {result['top_name'][:30]:30s} "
                  f"[{result['top_set']:4s}]  "
                  f"dist={result['top_dist']:.2f}")
            if not is_self:
                print(f"        expected found at rank "
                      f"{result['expected_rank']} "
                      f"dist={result['expected_dist']}")
        print()

    print("=" * 70)
    if all_ok:
        print("SELF-TEST PASSED. Hashing pipeline is internally consistent.")
        print("Bug is in the CAPTURE/WARP path (what the camera gives the")
        print("hasher doesn't match what was built from Scryfall PNGs).")
    else:
        print("SELF-TEST FAILED. Runtime hash pipeline diverged from build.")
        print("Rebuild the hash DB (create_card_hashes_v2.py) or check that")
        print("ART_REGION, CLAHE params, and channel order match between")
        print("hashing.py and create_card_hashes_v2.py.")
    print("=" * 70)


if __name__ == "__main__":
    main()
