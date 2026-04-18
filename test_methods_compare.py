# test_methods_compare.py
# ---------------------------------------------------------------------------
# Benchmarks different card detection method combinations to find what
# actually improves matching accuracy.
#
# Tests combinations of:
#   - Crop: art-only vs full 745x745
#   - Hash types: phash, dhash, whash, and combinations
#   - Preprocessing: CLAHE vs raw vs both averaged
#   - Re-ranking: histogram correlation vs none
#
# Usage:
#   python test_methods_compare.py              # live camera
#   python test_methods_compare.py <image_dir>  # batch test saved images
#
# Controls (camera mode):
#   SPACE  — Capture and compare all methods
#   ESC    — Quit
# ---------------------------------------------------------------------------

import os
import sys
import time
import cv2
import numpy as np
from PIL import Image
import imagehash

print("Loading files, please wait...")

from config import (
    HASH_DB_PATH, CARD_WIDTH, CARD_HEIGHT, ART_REGION,
    PHASH_DISTANCE_THRESHOLD, EXCLUDED_SETS,
)
from detection import load_bounding_box, crop_to_bounding_box, setup_bounding_box, determine_orientation
from cards import extract_card_info, CARD_DATA_BY_ID


# ---------------------------------------------------------------------------
# Load BOTH hash databases
# ---------------------------------------------------------------------------

HASH_DB_V1 = {}
HASH_DB_V2 = {}

v1_path = HASH_DB_PATH
v2_path = os.path.join(os.path.dirname(HASH_DB_PATH), "card_hashes_v2.json")

import json

if os.path.exists(v1_path):
    with open(v1_path, 'r', encoding='utf-8') as f:
        HASH_DB_V1 = json.load(f)
    print(f"[compare] Loaded v1 hash DB: {len(HASH_DB_V1)} entries")

if os.path.exists(v2_path):
    with open(v2_path, 'r', encoding='utf-8') as f:
        HASH_DB_V2 = json.load(f)
    print(f"[compare] Loaded v2 hash DB: {len(HASH_DB_V2)} entries")

if not HASH_DB_V1 and not HASH_DB_V2:
    print("ERROR: No hash databases found!")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Precompute hash objects from both DBs
# ---------------------------------------------------------------------------

def _hex(s):
    return imagehash.hex_to_hash(s)


# V1: phash only (full 745x745 crop, CLAHE-corrected)
V1_HASHES = []  # [(card_id, r_ph, g_ph, b_ph)]
for cid, h in HASH_DB_V1.items():
    try:
        V1_HASHES.append((cid, _hex(h['r_phash']), _hex(h['g_phash']), _hex(h['b_phash'])))
    except (ValueError, KeyError):
        pass

# V2: multi-hash (art crop, CLAHE-corrected)
V2_HASHES = []  # [(card_id, p_r, p_g, p_b, d_r, d_g, d_b, w_r, w_g, w_b)]
for cid, h in HASH_DB_V2.items():
    try:
        V2_HASHES.append((
            cid,
            _hex(h['p_r']), _hex(h['p_g']), _hex(h['p_b']),
            _hex(h['d_r']), _hex(h['d_g']), _hex(h['d_b']),
            _hex(h['w_r']), _hex(h['w_g']), _hex(h['w_b']),
        ))
    except (ValueError, KeyError):
        pass

print(f"[compare] V1: {len(V1_HASHES)} cards, V2: {len(V2_HASHES)} cards")


# ---------------------------------------------------------------------------
# CLAHE helper
# ---------------------------------------------------------------------------

def apply_clahe(pil_img, clip_limit=2.0, grid_size=8):
    img_bgr = cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
    lab = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2LAB)
    l_ch, a_ch, b_ch = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=clip_limit,
                             tileGridSize=(grid_size, grid_size))
    l_corrected = clahe.apply(l_ch)
    corrected_lab = cv2.merge([l_corrected, a_ch, b_ch])
    corrected_bgr = cv2.cvtColor(corrected_lab, cv2.COLOR_LAB2BGR)
    corrected_rgb = cv2.cvtColor(corrected_bgr, cv2.COLOR_BGR2RGB)
    return Image.fromarray(corrected_rgb)


# ---------------------------------------------------------------------------
# Histogram re-ranking
# ---------------------------------------------------------------------------

IMAGES_DIR = os.path.join(os.path.dirname(__file__), "downloaded_cards")


def compute_art_histogram(art_bgr):
    hsv = cv2.cvtColor(art_bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None,
                        [32, 32, 16], [0, 180, 0, 256, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def rerank_top(card_img, candidates, top_n=30):
    """Re-rank top candidates using histogram correlation on art region."""
    x1, y1, x2, y2 = ART_REGION
    h, w = card_img.shape[:2]
    query_art = card_img[y1:min(y2, h), x1:min(x2, w)]
    query_hist = compute_art_histogram(query_art)

    reranked = []
    for cid, hash_dist in candidates[:top_n]:
        ref_path = os.path.join(IMAGES_DIR, f"{cid}.png")
        if not os.path.exists(ref_path):
            reranked.append((cid, hash_dist))
            continue
        ref_img = cv2.imread(ref_path)
        if ref_img is None:
            reranked.append((cid, hash_dist))
            continue
        rh, rw = ref_img.shape[:2]
        ref_art = ref_img[y1:min(y2, rh), x1:min(x2, rw)]
        ref_hist = compute_art_histogram(ref_art)
        corr = cv2.compareHist(query_hist, ref_hist, cv2.HISTCMP_CORREL)
        hist_dist = (1.0 - corr) * 100.0
        combined = (hash_dist + hist_dist) / 2.0
        reranked.append((cid, combined))

    reranked.extend(candidates[top_n:])
    reranked.sort(key=lambda x: x[1])
    return reranked


# ---------------------------------------------------------------------------
# Crop helpers
# ---------------------------------------------------------------------------

def crop_art(card_img):
    """Crop art region from a BGR card image, return as PIL RGB."""
    x1, y1, x2, y2 = ART_REGION
    h, w = card_img.shape[:2]
    art = card_img[y1:min(y2, h), x1:min(x2, w)]
    return Image.fromarray(cv2.cvtColor(art, cv2.COLOR_BGR2RGB))


def crop_full(card_img, size=745):
    """Crop full 745x745 from top-left, return as PIL RGB."""
    crop = card_img[0:size, 0:size]
    return Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))


# ---------------------------------------------------------------------------
# Method definitions
# ---------------------------------------------------------------------------

def _hash_channels(pil_img, hash_types, hash_size=16):
    """
    Compute requested hash types for R, G, B channels.
    hash_types: subset of {'phash', 'dhash', 'whash'}
    Returns dict: {'phash': (r, g, b), 'dhash': (r, g, b), ...}
    """
    r, g, b = pil_img.convert('RGB').split()
    result = {}
    if 'phash' in hash_types:
        result['phash'] = (imagehash.phash(r, hash_size=hash_size),
                           imagehash.phash(g, hash_size=hash_size),
                           imagehash.phash(b, hash_size=hash_size))
    if 'dhash' in hash_types:
        result['dhash'] = (imagehash.dhash(r, hash_size=hash_size),
                           imagehash.dhash(g, hash_size=hash_size),
                           imagehash.dhash(b, hash_size=hash_size))
    if 'whash' in hash_types:
        result['whash'] = (imagehash.whash(r, hash_size=hash_size),
                           imagehash.whash(g, hash_size=hash_size),
                           imagehash.whash(b, hash_size=hash_size))
    return result


def run_method(card_img, method_cfg):
    """
    Run a single matching method configuration against the card image.

    method_cfg keys:
        name:        Display name
        crop:        'art' or 'full'
        hash_types:  set of 'phash', 'dhash', 'whash'
        preprocess:  'clahe', 'raw', or 'both'
        rerank:      True or False
        db:          'v1' or 'v2'

    Returns dict with:
        top_id, top_name, top_set, top_dist, gap_to_2nd, time_ms
    """
    t0 = time.perf_counter()

    # 1) Crop
    if method_cfg['crop'] == 'art':
        pil_img = crop_art(card_img)
    else:
        pil_img = crop_full(card_img)

    # 2) Preprocessing + hashing
    hash_types = method_cfg['hash_types']
    preprocess = method_cfg['preprocess']

    if preprocess == 'clahe':
        corrected = apply_clahe(pil_img)
        hashes = _hash_channels(corrected, hash_types)
    elif preprocess == 'raw':
        hashes = _hash_channels(pil_img, hash_types)
    else:  # 'both' — average raw + CLAHE distances
        hashes_raw = _hash_channels(pil_img, hash_types)
        hashes_clahe = _hash_channels(apply_clahe(pil_img), hash_types)
        hashes = {'raw': hashes_raw, 'clahe': hashes_clahe}

    # 3) Compare against DB
    db = method_cfg['db']
    results = []

    if db == 'v1' and V1_HASHES:
        # V1 only has phash
        if preprocess == 'both':
            pr_raw, pg_raw, pb_raw = hashes['raw']['phash']
            pr_cl, pg_cl, pb_cl = hashes['clahe']['phash']
            for entry in V1_HASHES:
                cid, s_r, s_g, s_b = entry
                d_raw = ((pr_raw - s_r) + (pg_raw - s_g) + (pb_raw - s_b)) / 3.0
                d_cl = ((pr_cl - s_r) + (pg_cl - s_g) + (pb_cl - s_b)) / 3.0
                results.append((cid, (d_raw + d_cl) / 2.0))
        else:
            pr, pg, pb = hashes['phash']
            for entry in V1_HASHES:
                cid, s_r, s_g, s_b = entry
                d = ((pr - s_r) + (pg - s_g) + (pb - s_b)) / 3.0
                results.append((cid, d))

    elif db == 'v2' and V2_HASHES:
        # V2 has phash + dhash + whash
        if preprocess == 'both':
            h_raw = hashes['raw']
            h_cl = hashes['clahe']
            for entry in V2_HASHES:
                cid = entry[0]
                dists = []
                if 'phash' in hash_types:
                    s_pr, s_pg, s_pb = entry[1], entry[2], entry[3]
                    d_raw = ((h_raw['phash'][0] - s_pr) + (h_raw['phash'][1] - s_pg) + (h_raw['phash'][2] - s_pb)) / 3.0
                    d_cl = ((h_cl['phash'][0] - s_pr) + (h_cl['phash'][1] - s_pg) + (h_cl['phash'][2] - s_pb)) / 3.0
                    dists.append((d_raw + d_cl) / 2.0)
                if 'dhash' in hash_types:
                    s_dr, s_dg, s_db = entry[4], entry[5], entry[6]
                    d_raw = ((h_raw['dhash'][0] - s_dr) + (h_raw['dhash'][1] - s_dg) + (h_raw['dhash'][2] - s_db)) / 3.0
                    d_cl = ((h_cl['dhash'][0] - s_dr) + (h_cl['dhash'][1] - s_dg) + (h_cl['dhash'][2] - s_db)) / 3.0
                    dists.append((d_raw + d_cl) / 2.0)
                if 'whash' in hash_types:
                    s_wr, s_wg, s_wb = entry[7], entry[8], entry[9]
                    d_raw = ((h_raw['whash'][0] - s_wr) + (h_raw['whash'][1] - s_wg) + (h_raw['whash'][2] - s_wb)) / 3.0
                    d_cl = ((h_cl['whash'][0] - s_wr) + (h_cl['whash'][1] - s_wg) + (h_cl['whash'][2] - s_wb)) / 3.0
                    dists.append((d_raw + d_cl) / 2.0)
                combined = sum(dists) / len(dists) if dists else 999
                results.append((cid, combined))
        else:
            for entry in V2_HASHES:
                cid = entry[0]
                dists = []
                if 'phash' in hash_types:
                    s_pr, s_pg, s_pb = entry[1], entry[2], entry[3]
                    d = ((hashes['phash'][0] - s_pr) + (hashes['phash'][1] - s_pg) + (hashes['phash'][2] - s_pb)) / 3.0
                    dists.append(d)
                if 'dhash' in hash_types:
                    s_dr, s_dg, s_db = entry[4], entry[5], entry[6]
                    d = ((hashes['dhash'][0] - s_dr) + (hashes['dhash'][1] - s_dg) + (hashes['dhash'][2] - s_db)) / 3.0
                    dists.append(d)
                if 'whash' in hash_types:
                    s_wr, s_wg, s_wb = entry[7], entry[8], entry[9]
                    d = ((hashes['whash'][0] - s_wr) + (hashes['whash'][1] - s_wg) + (hashes['whash'][2] - s_wb)) / 3.0
                    dists.append(d)
                combined = sum(dists) / len(dists) if dists else 999
                results.append((cid, combined))
    else:
        return None

    # Filter to paper cards, exclude promo sets
    allowed = []
    for cid, dist in results:
        cdata = CARD_DATA_BY_ID.get(cid, {})
        if 'paper' not in cdata.get('games', []):
            continue
        if cdata.get('set', '').lower() in EXCLUDED_SETS:
            continue
        allowed.append((cid, dist))
    allowed.sort(key=lambda x: x[1])

    if not allowed:
        return None

    # 4) Optional re-ranking
    if method_cfg.get('rerank') and os.path.isdir(IMAGES_DIR):
        allowed = rerank_top(card_img, allowed, top_n=30)

    t1 = time.perf_counter()

    top_id, top_dist = allowed[0]
    gap = allowed[1][1] - top_dist if len(allowed) > 1 else 999

    top_data = CARD_DATA_BY_ID.get(top_id, {})
    top_name = top_data.get('name', '?')
    top_set = top_data.get('set', '?')

    # Also get top 3 for detailed output
    top3 = []
    for cid, dist in allowed[:3]:
        cdata = CARD_DATA_BY_ID.get(cid, {})
        top3.append((cdata.get('name', '?'), cdata.get('set', '?'), dist))

    return {
        'top_id': top_id,
        'top_name': top_name,
        'top_set': top_set,
        'top_dist': top_dist,
        'gap': gap,
        'time_ms': (t1 - t0) * 1000,
        'top3': top3,
    }


# ---------------------------------------------------------------------------
# Method configurations to test
# ---------------------------------------------------------------------------

METHODS = []

# --- V1 DB methods (full crop, phash only) ---
if V1_HASHES:
    METHODS.extend([
        {
            'name': 'v1: full+phash+clahe',
            'crop': 'full', 'hash_types': {'phash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v1',
        },
        {
            'name': 'v1: full+phash+both',
            'crop': 'full', 'hash_types': {'phash'},
            'preprocess': 'both', 'rerank': False, 'db': 'v1',
        },
        {
            'name': 'v1: full+phash+raw',
            'crop': 'full', 'hash_types': {'phash'},
            'preprocess': 'raw', 'rerank': False, 'db': 'v1',
        },
    ])

# --- V2 DB methods (art crop, multi-hash) ---
if V2_HASHES:
    METHODS.extend([
        # Single hash types
        {
            'name': 'v2: art+phash+clahe',
            'crop': 'art', 'hash_types': {'phash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v2',
        },
        {
            'name': 'v2: art+dhash+clahe',
            'crop': 'art', 'hash_types': {'dhash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v2',
        },
        {
            'name': 'v2: art+whash+clahe',
            'crop': 'art', 'hash_types': {'whash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v2',
        },

        # Pair combinations
        {
            'name': 'v2: art+ph+dh+clahe',
            'crop': 'art', 'hash_types': {'phash', 'dhash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v2',
        },
        {
            'name': 'v2: art+ph+wh+clahe',
            'crop': 'art', 'hash_types': {'phash', 'whash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v2',
        },
        {
            'name': 'v2: art+dh+wh+clahe',
            'crop': 'art', 'hash_types': {'dhash', 'whash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v2',
        },

        # All three hashes
        {
            'name': 'v2: art+ALL+clahe',
            'crop': 'art', 'hash_types': {'phash', 'dhash', 'whash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v2',
        },

        # Preprocessing variants (all three hashes)
        {
            'name': 'v2: art+ALL+raw',
            'crop': 'art', 'hash_types': {'phash', 'dhash', 'whash'},
            'preprocess': 'raw', 'rerank': False, 'db': 'v2',
        },
        {
            'name': 'v2: art+ALL+both',
            'crop': 'art', 'hash_types': {'phash', 'dhash', 'whash'},
            'preprocess': 'both', 'rerank': False, 'db': 'v2',
        },

        # Re-ranking variants
        {
            'name': 'v2: art+ALL+clahe+rerank',
            'crop': 'art', 'hash_types': {'phash', 'dhash', 'whash'},
            'preprocess': 'clahe', 'rerank': True, 'db': 'v2',
        },
        {
            'name': 'v2: art+ALL+both+rerank',
            'crop': 'art', 'hash_types': {'phash', 'dhash', 'whash'},
            'preprocess': 'both', 'rerank': True, 'db': 'v2',
        },

        # Full crop with v2 DB (to isolate art crop improvement)
        {
            'name': 'v2: full+ALL+clahe',
            'crop': 'full', 'hash_types': {'phash', 'dhash', 'whash'},
            'preprocess': 'clahe', 'rerank': False, 'db': 'v2',
        },
    ])

print(f"[compare] {len(METHODS)} method configurations to test")


# ---------------------------------------------------------------------------
# Results display
# ---------------------------------------------------------------------------

def print_results(results, card_num=None):
    """Print a comparison table of all method results."""
    header = f"\n{'='*100}"
    if card_num is not None:
        header += f"\nCard #{card_num}"
    print(header)

    # Check if all methods agree on the same card
    names = set()
    for r in results:
        if r['result'] is not None:
            names.add(r['result']['top_name'])

    if len(names) == 1:
        print(f"ALL METHODS AGREE: {names.pop()}")
    elif len(names) > 1:
        print(f"DISAGREEMENT! Methods found: {names}")

    # Table header
    print()
    print(f"{'Method':<32} {'Top Match':<28} {'Set':<6} {'Dist':>7} {'Gap':>7} {'Time':>8}")
    print(f"{'-'*32} {'-'*28} {'-'*6} {'-'*7} {'-'*7} {'-'*8}")

    for r in results:
        name = r['name']
        res = r['result']
        if res is None:
            print(f"{name:<32} {'(no DB)':^28}")
            continue

        top = res['top_name']
        if len(top) > 27:
            top = top[:24] + "..."
        tset = res['top_set']
        dist = res['top_dist']
        gap = res['gap']
        ms = res['time_ms']

        # Color code: green if dist < threshold, red if not
        dist_str = f"{dist:7.2f}"
        gap_str = f"{gap:7.2f}" if gap < 500 else f"{'999+':>7}"

        print(f"{name:<32} {top:<28} {tset:<6} {dist_str} {gap_str} {ms:7.0f}ms")

    # Detailed top-3 for each method
    print(f"\n{'--- Top 3 per method ---':^100}")
    for r in results:
        res = r['result']
        if res is None:
            continue
        print(f"\n  {r['name']}:")
        for i, (cname, cset, cdist) in enumerate(res['top3'], 1):
            marker = " <-- BEST" if i == 1 else ""
            print(f"    #{i}: {cname} ({cset}) dist={cdist:.2f}{marker}")

    print(f"\n{'='*100}\n")


# ---------------------------------------------------------------------------
# Aggregate stats across multiple cards
# ---------------------------------------------------------------------------

class AggregateStats:
    """Track per-method stats across multiple test cards."""

    def __init__(self):
        self.method_stats = {}  # name -> {dists: [], gaps: [], times: [], correct: int, total: int}
        self.card_count = 0

    def record(self, results, known_name=None):
        """Record results for one card. known_name for accuracy tracking."""
        self.card_count += 1
        for r in results:
            name = r['name']
            res = r['result']
            if res is None:
                continue

            if name not in self.method_stats:
                self.method_stats[name] = {
                    'dists': [], 'gaps': [], 'times': [],
                    'correct': 0, 'total': 0, 'names': [],
                }

            stats = self.method_stats[name]
            stats['dists'].append(res['top_dist'])
            stats['gaps'].append(res['gap'])
            stats['times'].append(res['time_ms'])
            stats['total'] += 1
            stats['names'].append(res['top_name'])

            if known_name and res['top_name'].lower() == known_name.lower():
                stats['correct'] += 1

    def print_summary(self):
        """Print aggregate comparison table."""
        print(f"\n{'='*110}")
        print(f"AGGREGATE RESULTS ({self.card_count} cards)")
        print(f"{'='*110}")
        print()
        print(f"{'Method':<32} {'Avg Dist':>9} {'Avg Gap':>9} {'Min Gap':>9} "
              f"{'Avg ms':>8} {'Agree%':>8}")
        print(f"{'-'*32} {'-'*9} {'-'*9} {'-'*9} {'-'*8} {'-'*8}")

        # Find the most common name per card position to compute "agreement"
        # (what % of methods agree with the majority answer)
        majority_names = []
        if self.method_stats:
            first_method = list(self.method_stats.values())[0]
            for i in range(len(first_method['names'])):
                names_at_i = []
                for stats in self.method_stats.values():
                    if i < len(stats['names']):
                        names_at_i.append(stats['names'][i])
                if names_at_i:
                    from collections import Counter
                    majority = Counter(names_at_i).most_common(1)[0][0]
                    majority_names.append(majority)

        for name, stats in self.method_stats.items():
            avg_dist = np.mean(stats['dists']) if stats['dists'] else 0
            avg_gap = np.mean(stats['gaps']) if stats['gaps'] else 0
            min_gap = min(stats['gaps']) if stats['gaps'] else 0
            avg_time = np.mean(stats['times']) if stats['times'] else 0

            # Agreement: how often does this method match the majority vote?
            agree = 0
            for i, n in enumerate(stats['names']):
                if i < len(majority_names) and n == majority_names[i]:
                    agree += 1
            agree_pct = (agree / len(stats['names']) * 100) if stats['names'] else 0

            print(f"{name:<32} {avg_dist:9.2f} {avg_gap:9.2f} {min_gap:9.2f} "
                  f"{avg_time:7.0f}ms {agree_pct:7.1f}%")

        # Print which methods disagree with majority most often
        print()
        print("Disagreements with majority vote:")
        for name, stats in self.method_stats.items():
            disagreements = []
            for i, n in enumerate(stats['names']):
                if i < len(majority_names) and n != majority_names[i]:
                    disagreements.append((i + 1, n, majority_names[i]))
            if disagreements:
                print(f"  {name}: {len(disagreements)} disagreement(s)")
                for card_num, got, expected in disagreements[:5]:
                    print(f"    Card #{card_num}: got '{got}', majority='{expected}'")

        print(f"\n{'='*110}\n")


# ---------------------------------------------------------------------------
# Run comparison on a single card image
# ---------------------------------------------------------------------------

def compare_card(card_img):
    """Run all methods on a single card image. Returns list of results."""
    results = []
    for method_cfg in METHODS:
        res = run_method(card_img, method_cfg)
        results.append({'name': method_cfg['name'], 'result': res})
    return results


# ---------------------------------------------------------------------------
# Camera mode
# ---------------------------------------------------------------------------

def run_camera_mode():
    """Interactive camera mode: press SPACE to capture and compare."""
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Cannot open webcam.")
        return

    bounding_corners = load_bounding_box()
    if bounding_corners is None:
        print("No saved bounding box. Click 4 corners to set one.")
        bounding_corners = setup_bounding_box(cap)
        if bounding_corners is None:
            print("Cancelled.")
            cap.release()
            return

    print("\nReady.")
    print("  SPACE  — Capture and compare all methods")
    print("  b      — Reconfigure bounding box")
    print("  ESC    — Quit and see aggregate stats\n")

    agg = AggregateStats()
    card_num = 0
    cv2.namedWindow("Live View", cv2.WINDOW_AUTOSIZE)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

        display = frame.copy()
        if bounding_corners is not None:
            pts = bounding_corners.astype(int)
            for i in range(4):
                cv2.line(display, tuple(pts[i]), tuple(pts[(i + 1) % 4]),
                         (0, 255, 0), 2)

        h, w = display.shape[:2]
        cv2.putText(display, f"Cards tested: {card_num}", (10, 25),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 1)
        cv2.putText(display, "SPACE=capture | b=bbox | ESC=quit", (10, h - 15),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 150), 1)

        cv2.imshow("Live View", display)
        key = cv2.waitKey(1) & 0xFF

        if key == 27:  # ESC
            break

        elif key == ord('b'):
            bounding_corners = setup_bounding_box(cap)
            if bounding_corners is None:
                bounding_corners = load_bounding_box()

        elif key == 32:  # SPACE
            if bounding_corners is None:
                print("No bounding box. Press 'b' to set one.")
                continue

            # Grab multiple frames for auto-exposure
            for _ in range(5):
                ret, frame = cap.read()
            if not ret:
                continue
            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            card_img = crop_to_bounding_box(frame, bounding_corners)
            card_img, was_rotated = determine_orientation(card_img)
            if was_rotated:
                print("  (Card was upside-down, rotated 180)")

            cv2.imshow("Card", card_img)

            card_num += 1
            print(f"\nRunning {len(METHODS)} methods on card #{card_num}...")

            results = compare_card(card_img)
            print_results(results, card_num)
            agg.record(results)

    cap.release()
    cv2.destroyAllWindows()

    if card_num > 0:
        agg.print_summary()


# ---------------------------------------------------------------------------
# Batch mode (test saved images)
# ---------------------------------------------------------------------------

def run_batch_mode(image_dir):
    """Test all saved card images in a directory."""
    # Support both full frame images and pre-cropped card images
    files = sorted([f for f in os.listdir(image_dir)
                    if f.lower().endswith(('.png', '.jpg', '.jpeg'))])

    if not files:
        print(f"No images found in {image_dir}")
        return

    print(f"Found {len(files)} images in {image_dir}")

    bounding_corners = load_bounding_box()
    agg = AggregateStats()

    for idx, filename in enumerate(files, 1):
        filepath = os.path.join(image_dir, filename)
        img = cv2.imread(filepath)
        if img is None:
            print(f"  Skipping {filename} (can't read)")
            continue

        h, w = img.shape[:2]

        # Detect if this is a full frame or already-cropped card
        if w == CARD_WIDTH and h == CARD_HEIGHT:
            # Already a cropped card image
            card_img = img
        elif bounding_corners is not None:
            # Full frame — need to crop
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
            card_img = crop_to_bounding_box(img, bounding_corners)
        else:
            # Try to use as-is (might be a card photo)
            card_img = cv2.resize(img, (CARD_WIDTH, CARD_HEIGHT))

        card_img, _ = determine_orientation(card_img)

        print(f"\n[{idx}/{len(files)}] {filename}")
        results = compare_card(card_img)
        print_results(results, idx)
        agg.record(results)

    if agg.card_count > 0:
        agg.print_summary()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) > 1:
        image_dir = sys.argv[1]
        if os.path.isdir(image_dir):
            run_batch_mode(image_dir)
        elif os.path.isfile(image_dir):
            # Single file
            img = cv2.imread(image_dir)
            if img is not None:
                h, w = img.shape[:2]
                if w == CARD_WIDTH and h == CARD_HEIGHT:
                    card_img = img
                else:
                    card_img = cv2.resize(img, (CARD_WIDTH, CARD_HEIGHT))
                card_img, _ = determine_orientation(card_img)
                results = compare_card(card_img)
                print_results(results, 1)
            else:
                print(f"Cannot read image: {image_dir}")
        else:
            print(f"Not found: {image_dir}")
    else:
        run_camera_mode()


if __name__ == "__main__":
    main()
