#!/usr/bin/env python3
# update_all.py
# ---------------------------------------------------------------------------
# One-shot orchestrator for bringing the card-sorter reference data fully
# up to date:
#
#   1. Download latest Scryfall bulk-data JSON (default-cards-*.json)
#   2. Rebuild printings_map.json via the patched (illustration_id, frame)
#      dedup; compute the list of reference PNGs that should exist on disk
#   3. Download any missing PNGs into downloaded_cards/
#   4. Rebuild the phash DB  (card_hashes_v3.json + card_hashes_packed.npz)
#   5. Rebuild the DINOv2 embedding DB  (card_embeddings.npz)
#
# Each phase tracks elapsed time, reports work done vs skipped, and can be
# skipped individually if the inputs haven't changed since the last run. A
# phase that does work forces all downstream phases to rerun.
#
# Usage:
#   python update_all.py                       # normal run — skip phases whose
#                                              # inputs haven't changed
#   python update_all.py --force               # rebuild everything
#   python update_all.py --skip-bulk           # don't check Scryfall
#   python update_all.py --skip-embeddings     # phash only, no DINO
#   python update_all.py --dry-run             # report the plan, do nothing
# ---------------------------------------------------------------------------

import argparse
import glob
import json
import os
import re
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

REF_DIR = os.path.join(SCRIPT_DIR, "downloaded_cards")
PRINTINGS_MAP_PATH = os.path.join(SCRIPT_DIR, "printings_map.json")
HASH_DB_JSON = os.path.join(SCRIPT_DIR, "card_hashes_v3.json")
HASH_DB_NPZ = os.path.join(SCRIPT_DIR, "card_hashes_packed.npz")
EMBED_DB = os.path.join(SCRIPT_DIR, "card_embeddings.npz")
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.py")


# --- Display helpers -------------------------------------------------------

BAR = "=" * 72
SUBBAR = "-" * 72


def section(msg):
    print()
    print(BAR)
    print(f"  {msg}")
    print(BAR)


def phase_header(n, total, title):
    print()
    print(SUBBAR)
    print(f"  PHASE {n}/{total}: {title}")
    print(SUBBAR)


def fmt_time(sec):
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{sec / 60:.1f} min"


def mtime_or_zero(path):
    return os.path.getmtime(path) if os.path.exists(path) else 0


def latest_png_mtime():
    """Return mtime of the most-recently-modified PNG in downloaded_cards/."""
    if not os.path.isdir(REF_DIR):
        return 0
    latest = 0
    for name in os.listdir(REF_DIR):
        if name.endswith(".png"):
            m = os.path.getmtime(os.path.join(REF_DIR, name))
            if m > latest:
                latest = m
    return latest


# --- Phase 1: Scryfall bulk dump ------------------------------------------

def phase_scryfall_bulk(args):
    """Download the newest default-cards JSON if Scryfall has fresher data."""
    if args.skip_bulk:
        print("  --skip-bulk: leaving Scryfall bulk data as-is")
        return _newest_bulk_path(), False

    if args.dry_run:
        print("  (dry-run) would check Scryfall bulk-data catalog")
        return _newest_bulk_path(), False

    from download_cards import download_scryfall_bulk_data

    before = _newest_bulk_path()
    print("  Checking Scryfall bulk-data catalog...")
    path = download_scryfall_bulk_data()
    if not path:
        raise RuntimeError("Scryfall bulk download returned no path")

    is_new = (path != before)
    if is_new:
        print(f"  Updated bulk dump: {os.path.basename(path)}")
        _update_config_bulk_path(path)
    else:
        print(f"  Already current: {os.path.basename(path)}")
    return path, is_new


def _newest_bulk_path():
    paths = sorted(glob.glob(os.path.join(SCRIPT_DIR, "default-cards-*.json")))
    return paths[-1] if paths else None


def _update_config_bulk_path(new_path):
    """Rewrite CARDS_JSON_PATH in config.py to point at the new bulk file."""
    new_name = os.path.basename(new_path)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    new_src, n = re.subn(
        r'(CARDS_JSON_PATH\s*=\s*os\.path\.join\(SCRIPT_DIR,\s*")[^"]+(")',
        rf"\1{new_name}\2",
        src,
        count=1,
    )
    if n == 0:
        print("  WARN: could not locate CARDS_JSON_PATH in config.py — "
              "update manually")
        return
    if new_src != src:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            f.write(new_src)
        print(f"  Updated config.py CARDS_JSON_PATH -> {new_name}")


# --- Phase 2: Rebuild printings map / compute missing images -------------

def phase_build_printings_map(bulk_json_path, args):
    """Load the bulk dump, dedup by (illustration_id, frame), emit
    printings_map.json, and return the list of representative cards that
    still need to be downloaded.

    "Missing" is computed at the *bucket* level: a (illustration_id, frame)
    bucket is considered covered as long as at least one of its member
    card_ids has a PNG on disk. All cards in a bucket share title, mana
    cost, frame, and art (illustration_id), so they produce the same phash
    in Region A (which excludes the set symbol and collector number). We
    don't need to re-download the canonical "best" rep if some other
    printing of the same bucket is already present.
    """
    if not bulk_json_path:
        raise RuntimeError("No Scryfall bulk JSON available")

    print(f"  Loading {os.path.basename(bulk_json_path)}...")
    with open(bulk_json_path, "r", encoding="utf-8") as f:
        all_cards = json.load(f)
    print(f"  Loaded {len(all_cards)} raw entries")

    from download_cards import build_printings_map, _is_back_face_id
    try:
        from build_hash_db_v3 import should_exclude_card
    except ImportError:
        def should_exclude_card(c):
            return False

    t0 = time.time()
    rep_cards, printings_map = build_printings_map(all_cards)
    print(f"  Built printings map in {fmt_time(time.time() - t0)}")

    # Drop representatives that the downloader / hash builder would skip
    # anyway (oversized, tokens, emblems, memorabilia, minigames). These
    # count as "missing" under a naive filename check but are never
    # written to disk, which would create noise on every run.
    before = len(rep_cards)
    rep_cards = [c for c in rep_cards if not should_exclude_card(c)]
    excluded = before - len(rep_cards)
    if excluded:
        print(f"  Excluded {excluded} non-gameplay reps "
              f"(oversized / art_series / planar / scheme / vanguard / "
              f"memorabilia / minigame)")

    if args.dry_run:
        print("  (dry-run) would write printings_map.json")
    else:
        with open(PRINTINGS_MAP_PATH, "w", encoding="utf-8") as f:
            json.dump(printings_map, f, ensure_ascii=False, indent=2)
        print(f"  Wrote {PRINTINGS_MAP_PATH} ({len(printings_map)} entries)")

    # Diff reps vs disk, bucket-aware ------------------------------------
    os.makedirs(REF_DIR, exist_ok=True)
    present = {
        name.replace(".png", "")
        for name in os.listdir(REF_DIR)
        if name.endswith(".png")
    }

    # For every rep, look at the full (illust_id, frame) bucket via
    # printings_map.  If ANY member is on disk, the bucket is covered.
    # Back-face reps aren't in printings_map, so fall back to their own id.
    missing = []
    for rep in rep_cards:
        rep_id = rep["id"]
        if rep_id in present:
            continue
        # Bucket members: for front-face reps, printings_map[rep_id] has the
        # full printings list (all frames) — filter to same frame as the rep.
        entry = printings_map.get(rep_id)
        if entry:
            rep_frame = entry.get("frame", "")
            bucket_ids = {
                p["id"] for p in entry.get("printings", [])
                if p.get("frame", "") == rep_frame
            }
            if bucket_ids & present:
                continue  # covered by another printing
        elif _is_back_face_id(rep_id):
            # Back-face: no bucket info available — if exact id missing, it's
            # missing. (Back-face printings are rare and usually unique.)
            pass
        missing.append(rep)

    coverage = 100.0 * (len(rep_cards) - len(missing)) / max(1, len(rep_cards))
    print(f"  Representatives: {len(rep_cards)}  "
          f"| local PNGs: {len(present)}  | missing buckets: {len(missing)}  "
          f"| coverage: {coverage:.1f}%")
    return missing


# --- Phase 3: Download missing images -------------------------------------

def phase_download_missing(missing, args):
    if not missing:
        print("  No missing images — skipping download.")
        return 0, 0.0

    if args.dry_run:
        print(f"  (dry-run) would download {len(missing)} images")
        return 0, 0.0

    from download_cards import download_card_images

    t0 = time.time()
    # download_card_images is idempotent (skips existing) and already
    # prints progress every 500 items.
    download_card_images(missing, REF_DIR, image_size="png", calls_per_second=10)
    elapsed = time.time() - t0
    return len(missing), elapsed


# --- Phase 4: Rebuild phash DB --------------------------------------------

def phase_rebuild_phash(args, force):
    """Run build_hash_db_v3.py. Skip iff every PNG on disk is already in the
    existing JSON DB AND the DB is newer than the newest PNG."""
    if args.dry_run:
        print("  (dry-run) would run build_hash_db_v3.py")
        return 0.0, False

    if not force and _phash_db_up_to_date():
        print("  phash DB is newer than every PNG on disk — skipping rebuild.")
        return 0.0, True

    # Invalidate the packed npz so card_identify.py doesn't load a stale
    # cache; it will rebuild the cache from the new JSON on first use.
    if os.path.exists(HASH_DB_NPZ):
        try:
            os.remove(HASH_DB_NPZ)
            print(f"  Removed stale {os.path.basename(HASH_DB_NPZ)}")
        except OSError as e:
            print(f"  WARN: could not remove {HASH_DB_NPZ}: {e}")

    t0 = time.time()
    cmd = [sys.executable, "-u", "build_hash_db_v3.py"]
    proc = subprocess.run(cmd, cwd=SCRIPT_DIR)
    if proc.returncode != 0:
        raise RuntimeError(
            f"build_hash_db_v3.py failed with exit code {proc.returncode}")
    return time.time() - t0, False


def _phash_db_up_to_date():
    if not os.path.exists(HASH_DB_JSON):
        return False
    # DB must be newer than the newest PNG
    return mtime_or_zero(HASH_DB_JSON) >= latest_png_mtime()


# --- Phase 5: Rebuild embedding DB ----------------------------------------

def phase_rebuild_embeddings(args, force):
    if args.skip_embeddings:
        print("  --skip-embeddings: leaving embedding DB as-is")
        return 0.0, True
    if args.dry_run:
        print("  (dry-run) would run build_embedding_db.py")
        return 0.0, False

    if not force and _embed_db_up_to_date():
        print("  embedding DB is newer than every PNG on disk — skipping rebuild.")
        return 0.0, True

    t0 = time.time()
    cmd = [sys.executable, "-u", "build_embedding_db.py"]
    proc = subprocess.run(cmd, cwd=SCRIPT_DIR)
    if proc.returncode != 0:
        raise RuntimeError(
            f"build_embedding_db.py failed with exit code {proc.returncode}")
    return time.time() - t0, False


def _embed_db_up_to_date():
    if not os.path.exists(EMBED_DB):
        return False
    return mtime_or_zero(EMBED_DB) >= latest_png_mtime()


# --- Main ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Full data update: Scryfall + images + phash + embeddings")
    parser.add_argument("--force", action="store_true",
                        help="Force every phase to rebuild, even if unchanged")
    parser.add_argument("--skip-bulk", action="store_true",
                        help="Don't check Scryfall for a newer bulk dump")
    parser.add_argument("--skip-embeddings", action="store_true",
                        help="Don't rebuild DINOv2 embeddings")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report the plan but don't modify anything")
    args = parser.parse_args()

    overall_t0 = time.time()
    section("Card Sorter — Full Data Update")
    print(f"  Script dir:  {SCRIPT_DIR}")
    print(f"  Images dir:  {REF_DIR}")
    print(f"  Force: {args.force}   Dry-run: {args.dry_run}   "
          f"Skip-bulk: {args.skip_bulk}   Skip-embeddings: {args.skip_embeddings}")

    phase_times = []  # list of (label, elapsed_seconds, did_work)
    forced_downstream = args.force

    # ----- Phase 1 --------------------------------------------------------
    phase_header(1, 5, "Scryfall bulk data")
    t0 = time.time()
    bulk_path, bulk_updated = phase_scryfall_bulk(args)
    elapsed = time.time() - t0
    phase_times.append(("Scryfall bulk", elapsed, bulk_updated))
    if bulk_updated:
        forced_downstream = True
    print(f"  Phase done in {fmt_time(elapsed)}")

    # ----- Phase 2 --------------------------------------------------------
    phase_header(2, 5, "printings_map & missing-image diff")
    t0 = time.time()
    missing = phase_build_printings_map(bulk_path, args)
    elapsed = time.time() - t0
    phase_times.append(("printings_map", elapsed, True))
    print(f"  Phase done in {fmt_time(elapsed)}")

    # ----- Phase 3 --------------------------------------------------------
    phase_header(3, 5, "Download missing PNGs")
    t0 = time.time()
    n_downloaded, elapsed = phase_download_missing(missing, args)
    phase_times.append(("download images", elapsed, n_downloaded > 0))
    if n_downloaded > 0:
        forced_downstream = True
        print(f"  Downloaded {n_downloaded} images in {fmt_time(elapsed)}")
    else:
        print(f"  Nothing to download ({fmt_time(elapsed)})")

    # ----- Phase 4 --------------------------------------------------------
    phase_header(4, 5, "Rebuild phash DB  (card_hashes_v3.json)")
    t0 = time.time()
    elapsed, skipped = phase_rebuild_phash(args, force=forced_downstream)
    phase_times.append(("phash rebuild", elapsed, not skipped))
    print(f"  Phase done in {fmt_time(elapsed)}"
          + (" (skipped)" if skipped else ""))

    # ----- Phase 5 --------------------------------------------------------
    phase_header(5, 5, "Rebuild DINOv2 embeddings  (card_embeddings.npz)")
    t0 = time.time()
    elapsed, skipped = phase_rebuild_embeddings(args, force=forced_downstream)
    phase_times.append(("embeddings rebuild", elapsed, not skipped))
    print(f"  Phase done in {fmt_time(elapsed)}"
          + (" (skipped)" if skipped else ""))

    # ----- Summary --------------------------------------------------------
    total = time.time() - overall_t0
    section("Update complete")
    print(f"  Total elapsed: {fmt_time(total)}")
    print()
    print(f"  {'phase':<22}  {'elapsed':>10}  result")
    print(f"  {'-' * 22}  {'-' * 10}  ------")
    for label, e, did in phase_times:
        tag = "worked" if did else "skipped"
        print(f"  {label:<22}  {fmt_time(e):>10}  {tag}")

    if args.dry_run:
        print()
        print("  (dry-run — no files were modified)")

    # Quick verification hints
    if not args.dry_run:
        print()
        print("  Next steps:")
        print("    python test_regression_362.py    # measure accuracy delta")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(130)
    except Exception as e:
        print(f"\n[FATAL] {type(e).__name__}: {e}")
        sys.exit(1)
