#!/usr/bin/env python3
# test_grayscale_phash_foil.py
# -----------------------------------------------------------------------
# Test whether grayscale phash can rescue identification on foil cards.
# Specifically: scan 205 (foil Zombie Infestation) currently matches
# "Pile On" with the RGB phash pipeline. Does grayscale phash find the
# correct card at a confident distance?
# -----------------------------------------------------------------------

import os
import sys
import cv2
import time
import numpy as np
from PIL import Image
import imagehash

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from card_detect import detect_card
from cards import CARD_DATA_BY_ID

HASH_SIZE = 16   # match current DB (256-bit)
REGION_A = (30, 105, 715, 520)  # art region (from card_identify.py)

SESSION_DIR = os.path.join(
    SCRIPT_DIR, "scan_logs", "session_20260415_130451")
FRAMES_DIR = os.path.join(SESSION_DIR, "scan_images")
REF_DIR = os.path.join(SCRIPT_DIR, "downloaded_cards")


def hash_region_rgb(img_pil, region):
    """Per-channel (R,G,B) phash. Returns 3 x (HASH_SIZE*HASH_SIZE) bool array."""
    x1, y1, x2, y2 = region
    w, h = img_pil.size
    crop = img_pil.crop((x1, y1, min(x2, w), min(y2, h)))
    r, g, b = crop.split()
    return np.stack([
        imagehash.phash(r, hash_size=HASH_SIZE).hash.flatten(),
        imagehash.phash(g, hash_size=HASH_SIZE).hash.flatten(),
        imagehash.phash(b, hash_size=HASH_SIZE).hash.flatten(),
    ])


def hash_region_gray(img_pil, region):
    """Grayscale phash. Returns single (HASH_SIZE*HASH_SIZE) bool array."""
    x1, y1, x2, y2 = region
    w, h = img_pil.size
    crop = img_pil.crop((x1, y1, min(x2, w), min(y2, h)))
    gray = crop.convert('L')
    return imagehash.phash(gray, hash_size=HASH_SIZE).hash.flatten()


def bool_dist(a, b):
    return int(np.logical_xor(a, b).sum())


def try_rotations(warped_bgr):
    """Return list of (orientation, PIL image) for 0 and 180 deg."""
    up = Image.fromarray(cv2.cvtColor(warped_bgr, cv2.COLOR_BGR2RGB))
    rot = up.rotate(180)
    return [(0, up), (180, rot)]


def main():
    # 1) Warp scan 205
    frame = cv2.imread(os.path.join(FRAMES_DIR, "scan_0205.jpg"))
    card = detect_card(frame)
    assert card is not None, "scan 205 failed to detect"

    # 2) Compute both hashes for both orientations
    rgb_hashes = {}
    gray_hashes = {}
    for rot, pil in try_rotations(card):
        rgb_hashes[rot] = hash_region_rgb(pil, REGION_A)
        gray_hashes[rot] = hash_region_gray(pil, REGION_A)

    # 3) Reference set: we want to test a subset of cards and compare
    #    where Zombie Infestation ranks vs the "wrong" card Pile On.
    #    Build a limited comparison:  load ~500 cards + specifically
    #    add all Zombie Infestation printings + Pile On.
    zombie_ids = [
        'ccd5f98a-7ab5-44b3-850c-b50963dace66',  # ody
        'e1078bef-8caf-4dc5-a055-e64314501b23',  # arc
        'c84a3e27-841a-4eb5-afcd-ddb87d4280f7',  # m12
        'e2709a7b-734e-45df-beb4-950b102d42e9',  # pd3
        'be524227-8adc-4b01-808f-5ec4ab9dc09c',  # c19
        'c9ce5007-56ab-4361-8130-df48add1492b',  # jmp
        'ef2c8aca-3ade-46b0-8366-ab3b244eca01',  # dmr (EXPECTED)
        '12fd6d4d-203b-4a2e-8726-1533fe74e475',  # plst
    ]
    # Find Pile On
    pile_on_ids = [
        cid for cid, info in CARD_DATA_BY_ID.items()
        if info.get('name', '').lower() == 'pile on'
    ]
    print(f"Zombie Infestation printings: {len(zombie_ids)}")
    print(f"Pile On printings:            {len(pile_on_ids)}")

    # Random sample of ~1000 others
    all_ids = list(CARD_DATA_BY_ID.keys())
    import random
    random.seed(42)
    sample = random.sample(all_ids, 1000)
    test_ids = set(zombie_ids) | set(pile_on_ids) | set(sample)
    print(f"Test set size:                {len(test_ids)}")

    # 4) Hash each reference and compute distances
    print("\nHashing references...")
    t0 = time.time()
    ref_data = []  # list of (cid, name, rgb_hash, gray_hash)
    for i, cid in enumerate(test_ids):
        path = os.path.join(REF_DIR, f"{cid}.png")
        if not os.path.exists(path):
            continue
        try:
            pil = Image.open(path).convert('RGB')
        except Exception:
            continue
        w, h = pil.size
        if (w, h) != (745, 1040):
            pil = pil.resize((745, 1040), Image.LANCZOS)
        rgb_h = hash_region_rgb(pil, REGION_A)
        gray_h = hash_region_gray(pil, REGION_A)
        name = CARD_DATA_BY_ID[cid].get('name', '?')
        ref_data.append((cid, name, rgb_h, gray_h))
        if (i + 1) % 200 == 0:
            print(f"  {i+1}/{len(test_ids)} hashed ({time.time()-t0:.1f}s)")

    print(f"Hashed {len(ref_data)} references in {time.time()-t0:.1f}s")

    # 5) For each orientation, compute distances and rank
    for rot in [0, 180]:
        scan_rgb = rgb_hashes[rot]
        scan_gray = gray_hashes[rot]

        rgb_ranks = []  # (cid, name, avg_dist)
        gray_ranks = []
        for cid, name, rgb_h, gray_h in ref_data:
            rgb_dist = (
                bool_dist(scan_rgb[0], rgb_h[0])
                + bool_dist(scan_rgb[1], rgb_h[1])
                + bool_dist(scan_rgb[2], rgb_h[2])
            ) / 3.0
            rgb_ranks.append((cid, name, rgb_dist))
            gray_dist = bool_dist(scan_gray, gray_h)
            gray_ranks.append((cid, name, gray_dist))

        rgb_ranks.sort(key=lambda x: x[2])
        gray_ranks.sort(key=lambda x: x[2])

        print(f"\n{'=' * 70}")
        print(f"ROTATION {rot}°")
        print(f"{'=' * 70}")
        print(f"\nRGB phash (current method) - top 10:")
        for i, (cid, name, d) in enumerate(rgb_ranks[:10]):
            star = (" <-- ZI" if "zombie infestation" in name.lower()
                    else (" <-- PO" if "pile on" in name.lower() else ""))
            print(f"  {i+1:3d}. {d:6.2f}  {name[:40]:<40} "
                  f"{cid[:8]}{star}")

        print(f"\nGrayscale phash - top 10:")
        for i, (cid, name, d) in enumerate(gray_ranks[:10]):
            star = (" <-- ZI" if "zombie infestation" in name.lower()
                    else (" <-- PO" if "pile on" in name.lower() else ""))
            print(f"  {i+1:3d}. {d:6.2f}  {name[:40]:<40} "
                  f"{cid[:8]}{star}")

        # Find where each Zombie Infestation printing ranks
        print(f"\nZombie Infestation printings (lower rank = better):")
        print(f"  {'cid':<10} {'set':>4}  {'rgb_dist':>8} {'rgb_rank':>8}  "
              f"{'gray_dist':>9} {'gray_rank':>9}")
        for zid in zombie_ids:
            set_code = CARD_DATA_BY_ID[zid].get('set', '?')
            # Find in ranks
            rgb_pos = next(
                (i + 1 for i, (cid, _, _) in enumerate(rgb_ranks) if cid == zid),
                None)
            rgb_d = next(
                (d for cid, _, d in rgb_ranks if cid == zid), None)
            gray_pos = next(
                (i + 1 for i, (cid, _, _) in enumerate(gray_ranks) if cid == zid),
                None)
            gray_d = next(
                (d for cid, _, d in gray_ranks if cid == zid), None)
            print(f"  {zid[:8]:<10} {set_code:>4}  "
                  f"{'' if rgb_d is None else f'{rgb_d:8.2f}'} "
                  f"{'-' if rgb_pos is None else str(rgb_pos):>8}  "
                  f"{'' if gray_d is None else f'{gray_d:9.2f}'} "
                  f"{'-' if gray_pos is None else str(gray_pos):>9}")


if __name__ == '__main__':
    main()
