#!/usr/bin/env python3
# audit_frame_coverage.py
# ---------------------------------------------------------------------------
# The download pipeline (download_cards.py) dedupes by illustration_id — one
# image per unique artwork. That's wrong when a card has been reprinted with
# different FRAMES (e.g. Zombie Infestation: 1997, 2003, 2015 — same art, 3
# frames). phash Region A (30,105,715,520) includes title + mana cost + top
# border, so different frames produce substantially different hashes for the
# SAME art.
#
# This script audits:
#   1. How many illustration_ids span multiple frames
#   2. For each such group, which frame is present locally and which are
#      missing
#   3. A prioritized download list (by frame recency / set recency)
# ---------------------------------------------------------------------------

import os
import sys
import json
import glob
from collections import defaultdict, Counter

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

REF_DIR = os.path.join(SCRIPT_DIR, "downloaded_cards")


def load_bulk_cards():
    """Load the newest default-cards-*.json file (full Scryfall dump)."""
    candidates = sorted(glob.glob(os.path.join(SCRIPT_DIR, "default-cards-*.json")))
    if not candidates:
        print("ERROR: no default-cards-*.json in Scripts/. "
              "Run download_cards.py first.")
        sys.exit(1)
    path = candidates[-1]
    print(f"Loading {os.path.basename(path)} ...")
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def is_downloadable(card):
    """Roughly match download_cards.py's filter."""
    if card.get('lang') != 'en':
        return False
    games = card.get('games') or []
    if 'paper' not in games:
        return False
    # Skip tokens, art series, etc.
    if card.get('layout') in {'token', 'double_faced_token', 'art_series',
                              'emblem', 'vanguard', 'scheme', 'planar'}:
        return False
    if 'image_uris' not in card and 'card_faces' not in card:
        return False
    return True


def main():
    all_cards = load_bulk_cards()
    print(f"Loaded {len(all_cards)} raw entries")

    # Build present-file set
    present_ids = set(
        f.replace('.png', '').replace('__back', '')
        for f in os.listdir(REF_DIR)
        if f.endswith('.png')
    )
    print(f"Local downloaded_cards/: {len(present_ids)} files")

    # Group by illustration_id → list of (card_id, set, frame, collector_num)
    groups = defaultdict(list)
    for c in all_cards:
        if not is_downloadable(c):
            continue
        illust = c.get('illustration_id')
        if not illust:
            continue
        groups[illust].append({
            'id': c['id'],
            'set': c.get('set', '?'),
            'set_name': c.get('set_name', '?'),
            'frame': c.get('frame', '?'),
            'border': c.get('border_color', '?'),
            'released_at': c.get('released_at', ''),
            'collector_number': c.get('collector_number', ''),
            'name': c.get('name', '?'),
        })

    print(f"Unique illustration_ids: {len(groups)}")

    # Find illustrations spanning multiple frames
    multi_frame_groups = {
        illust: items
        for illust, items in groups.items()
        if len({it['frame'] for it in items}) > 1
    }
    print(f"Illustrations with multiple frames: {len(multi_frame_groups)}")

    # For each multi-frame group, find which frames we have locally
    missing_by_frame = defaultdict(list)  # frame -> list of (card_id, name, set)
    groups_fully_covered = 0
    groups_partial = 0
    groups_none = 0

    for illust, items in multi_frame_groups.items():
        # Organize items by frame — pick one representative card_id per frame
        by_frame = defaultdict(list)
        for it in items:
            by_frame[it['frame']].append(it)

        frames_present = set()
        for frame, frame_items in by_frame.items():
            # Any printing of this frame present?
            if any(it['id'] in present_ids for it in frame_items):
                frames_present.add(frame)

        frames_missing = set(by_frame.keys()) - frames_present

        if not frames_missing:
            groups_fully_covered += 1
            continue

        if not frames_present:
            groups_none += 1
        else:
            groups_partial += 1

        # For each missing frame, pick the BEST representative (most recent
        # English paper printing in that frame)
        for frame in frames_missing:
            frame_items = by_frame[frame]
            # Prefer: non-promo, most recent
            frame_items.sort(
                key=lambda it: (it.get('released_at', ''), it['set']),
                reverse=True,
            )
            best = frame_items[0]
            missing_by_frame[frame].append(best)

    print()
    print(f"Multi-frame illustrations — coverage breakdown:")
    print(f"  fully covered (all frames present): {groups_fully_covered}")
    print(f"  partial (some frames missing):      {groups_partial}")
    print(f"  none (no frame present):            {groups_none}")
    print()

    total_missing = sum(len(v) for v in missing_by_frame.values())
    print(f"Total missing (illustration, frame) pairs to download: {total_missing}")
    print()
    print(f"Breakdown by frame:")
    for frame in sorted(missing_by_frame, key=lambda f: -len(missing_by_frame[f])):
        print(f"  frame {frame}: {len(missing_by_frame[frame])} missing")

    # Show sample from each frame
    print()
    print(f"Sample missing cards per frame:")
    for frame in sorted(missing_by_frame, key=lambda f: -len(missing_by_frame[f])):
        items = missing_by_frame[frame]
        print(f"\n  frame {frame} ({len(items)} total):")
        for it in items[:5]:
            print(f"    {it['name']:<35} {it['set']:>5}  "
                  f"(#{it['collector_number']}  released {it['released_at']})")

    # Write the full missing list as a JSON file for downloading
    out_path = os.path.join(SCRIPT_DIR, "missing_frame_printings.json")
    flat_missing = []
    for frame, items in missing_by_frame.items():
        for it in items:
            flat_missing.append({
                'id': it['id'],
                'name': it['name'],
                'set': it['set'],
                'frame': frame,
                'collector_number': it['collector_number'],
                'released_at': it['released_at'],
            })
    # Sort by released_at descending — fetch newest first
    flat_missing.sort(key=lambda x: x.get('released_at', ''), reverse=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(flat_missing, f, indent=2, ensure_ascii=False)
    print(f"\nWrote {len(flat_missing)} missing entries to {out_path}")
    print(f"Sorted by release date descending (newest first).")


if __name__ == '__main__':
    main()
