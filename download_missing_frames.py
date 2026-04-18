#!/usr/bin/env python3
# download_missing_frames.py
# ---------------------------------------------------------------------------
# Consumes missing_frame_printings.json (from audit_frame_coverage.py) and
# downloads the PNGs into downloaded_cards/.
#
# Pulls image_uris directly from the Scryfall bulk default-cards JSON so we
# don't need to hit the API per-card — saves ~7500 API calls.
# ---------------------------------------------------------------------------

import os
import sys
import json
import time
import glob
import argparse
import requests

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

REF_DIR = os.path.join(SCRIPT_DIR, "downloaded_cards")
MISSING_PATH = os.path.join(SCRIPT_DIR, "missing_frame_printings.json")


def load_bulk_image_index():
    """Build {card_id: image_uri_png} from the newest Scryfall bulk dump."""
    paths = sorted(glob.glob(os.path.join(SCRIPT_DIR, "default-cards-*.json")))
    if not paths:
        print("ERROR: no default-cards-*.json in Scripts/")
        sys.exit(1)
    path = paths[-1]
    print(f"Loading image index from {os.path.basename(path)} ...")
    with open(path, 'r', encoding='utf-8') as f:
        cards = json.load(f)

    index = {}
    for c in cards:
        cid = c.get('id')
        if not cid:
            continue
        uris = c.get('image_uris')
        if uris and 'png' in uris:
            index[cid] = uris['png']
            continue
        # Multi-face — use face 0's png
        faces = c.get('card_faces') or []
        if faces and 'image_uris' in faces[0]:
            f0 = faces[0]['image_uris']
            if 'png' in f0:
                index[cid] = f0['png']
    print(f"Indexed {len(index)} card_id -> png_url mappings")
    return index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=None,
                        help='Only download first N entries (for testing)')
    parser.add_argument('--rate', type=float, default=10.0,
                        help='Requests per second (default 10, Scryfall limit)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Just print the plan, download nothing')
    args = parser.parse_args()

    if not os.path.exists(MISSING_PATH):
        print(f"ERROR: {MISSING_PATH} not found. "
              f"Run audit_frame_coverage.py first.")
        sys.exit(1)

    with open(MISSING_PATH, 'r', encoding='utf-8') as f:
        missing = json.load(f)
    print(f"Missing list: {len(missing)} entries")

    if args.limit:
        missing = missing[:args.limit]
        print(f"Limited to first {len(missing)}")

    # Filter out any that already exist on disk (idempotent resume)
    existing = set(
        f.replace('.png', '')
        for f in os.listdir(REF_DIR)
        if f.endswith('.png')
    )
    to_do = [m for m in missing if m['id'] not in existing]
    skipped_existing = len(missing) - len(to_do)
    print(f"Already on disk: {skipped_existing}   To download: {len(to_do)}")

    if not to_do:
        print("Nothing to do.")
        return

    index = load_bulk_image_index()

    # Plan check: how many have image_uris?
    with_uri = [m for m in to_do if m['id'] in index]
    without_uri = [m for m in to_do if m['id'] not in index]
    print(f"With image_uri: {len(with_uri)}   "
          f"Without (skipped): {len(without_uri)}")

    if args.dry_run:
        print("--dry-run — exiting without downloading.")
        return

    delay = 1.0 / args.rate
    downloaded = 0
    errors = 0
    t0 = time.time()

    session = requests.Session()
    session.headers.update({'User-Agent': 'CardSorter/1.0 (frame-coverage-fill)'})

    print(f"\nDownloading {len(with_uri)} images at ~{args.rate}/s...")
    for i, m in enumerate(with_uri, start=1):
        cid = m['id']
        url = index[cid]
        out_path = os.path.join(REF_DIR, f"{cid}.png")
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            with open(out_path, 'wb') as f:
                f.write(r.content)
            downloaded += 1
        except Exception as e:
            print(f"  [ERR] {cid[:8]} {m.get('name','?'):<30} -> {e}")
            errors += 1

        if i % 200 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (len(with_uri) - i) / rate if rate else 0
            print(f"  [{i}/{len(with_uri)}] downloaded={downloaded} "
                  f"errors={errors}  rate={rate:.1f}/s  eta={eta/60:.1f}min")

        time.sleep(delay)

    elapsed = time.time() - t0
    print(f"\nDone in {elapsed/60:.1f} min: {downloaded} downloaded, "
          f"{errors} errors")
    print()
    print("Next steps:")
    print("  1. python build_hash_db_v3.py       # rebuild hash DB with new images")
    print("  2. python test_regression_362.py    # verify accuracy delta")


if __name__ == '__main__':
    main()
