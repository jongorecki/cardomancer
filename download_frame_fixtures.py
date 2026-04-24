# download_frame_fixtures.py
# ---------------------------------------------------------------------------
# Download Scryfall PNGs for the frame-classifier fixture set.
#
# For each "anchor" illustration (shared across >=2 of our target frame
# signatures — 1993, 1997, 2003, 2015, borderless), pick one representative
# printing per frame-sig and download the high-res PNG into
# tests/fixtures/frame_classifier/<sig>/<name>_<set>_<col>_<id8>.png.
#
# IMPORTANT: does NOT touch downloaded_cards/ or the hash DB. These
# fixture images are separate from the identification hash pipeline on
# purpose — we are only using them to tune / test the frame classifier.
#
# Usage:
#   python download_frame_fixtures.py              # download anything missing
#   python download_frame_fixtures.py --dry-run    # print plan, no downloads
#   python download_frame_fixtures.py --anchors 50 # smaller anchor set
# ---------------------------------------------------------------------------

import argparse
import csv
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cards import CARDS_DATA  # noqa: E402

RATE_DELAY_SEC = 0.1           # Scryfall asks for <=10 req/s.
TIMEOUT_SEC = 30
USER_AGENT = "CardSorter-FrameFixtureDownloader/1.0"

OUT_BASE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "tests", "fixtures", "frame_classifier",
)

EXCLUDED_LAYOUTS = {
    "art_series", "planar", "scheme", "vanguard", "token", "emblem",
}
TARGET_SIGS = ("1993", "1997", "2003", "2015", "borderless")


def frame_sig(card: dict) -> str:
    """Map a Scryfall card dict to its target frame signature."""
    bc = card.get("border_color", "")
    effects = card.get("frame_effects", []) or []
    if bc == "borderless" or "borderless" in effects:
        return "borderless"
    return card.get("frame", "?")


def safe_name(name: str, limit: int = 30) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in name)[:limit]


def get_png_url(card: dict) -> str:
    iu = card.get("image_uris") or {}
    url = iu.get("png")
    if url:
        return url
    # Dual-face: Scryfall puts image_uris on each face instead of the card.
    faces = card.get("card_faces") or []
    if faces:
        face_iu = faces[0].get("image_uris") or {}
        return face_iu.get("png") or ""
    return ""


def pick_fixture_plan(top_n_anchors: int, extra_borderless: int) -> list:
    """Return list of (name, sig, card_dict) — one pick per (anchor, sig)."""
    by_illus = defaultdict(list)
    for c in CARDS_DATA:
        iid = c.get("illustration_id")
        if iid and c.get("lang") == "en" and "paper" in c.get("games", []):
            by_illus[iid].append(c)

    anchors = []
    for iid, cards in by_illus.items():
        cards = [c for c in cards if c.get("layout", "") not in EXCLUDED_LAYOUTS]
        sigs = set(frame_sig(c) for c in cards) & set(TARGET_SIGS)
        if len(sigs) < 2:
            continue
        anchors.append((len(sigs), len(cards), iid,
                        cards[0].get("name", ""), sigs, cards))
    anchors.sort(key=lambda x: (-x[0], -x[1]))

    selected = list(anchors[:top_n_anchors])
    # Ensure we include borderless-covering anchors even if they rank lower.
    seen_iids = {a[2] for a in selected}
    borderless_pool = [a for a in anchors if "borderless" in a[4]]
    for a in borderless_pool[:extra_borderless]:
        if a[2] not in seen_iids:
            selected.append(a)
            seen_iids.add(a[2])

    # For each (anchor, target_sig), pick one printing. Prefer non-promo,
    # low collector number as a proxy for "canonical" printing.
    rows = []
    for _, _, _, name, _, cards in selected:
        by_s = defaultdict(list)
        for c in cards:
            s = frame_sig(c)
            if s in TARGET_SIGS:
                by_s[s].append(c)
        for s, clist in by_s.items():
            clist.sort(key=lambda c: (
                c.get("promo", False),
                c.get("set_type", "") == "promo",
                c.get("collector_number", ""),
            ))
            rows.append((name, s, clist[0]))
    return rows


def download_one(url: str, dst: str) -> None:
    """Atomic write: download to .tmp, then rename."""
    tmp = dst + ".tmp"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        data = resp.read()
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, dst)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--anchors", type=int, default=100,
                    help="Take the top N shared-art anchors (default 100).")
    ap.add_argument("--extra-borderless", type=int, default=16,
                    help="Additional borderless-covering anchors beyond top-N.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan + what would be downloaded, no HTTP.")
    args = ap.parse_args(argv)

    for s in TARGET_SIGS:
        os.makedirs(os.path.join(OUT_BASE, s), exist_ok=True)

    picks = pick_fixture_plan(args.anchors, args.extra_borderless)
    print(f"[plan] {len(picks)} (anchor, sig) fixture rows")

    plan_rows = []
    todo = []
    already_present = 0
    no_url = 0
    for name, sig, card in picks:
        cid = card["id"]
        filename = (
            f"{safe_name(name)}_{card['set']}_{card['collector_number']}"
            f"_{cid[:8]}.png"
        )
        dst = os.path.join(OUT_BASE, sig, filename)
        plan_rows.append([
            name, sig, card["set"], card["collector_number"], cid, filename,
        ])
        if os.path.exists(dst):
            already_present += 1
            continue
        url = get_png_url(card)
        if not url:
            no_url += 1
            continue
        todo.append((url, dst, sig, name))

    print(f"[plan] already present: {already_present}")
    print(f"[plan] to download:     {len(todo)}")
    print(f"[plan] no PNG url:      {no_url}")

    plan_path = os.path.join(OUT_BASE, "plan.csv")
    with open(plan_path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["name", "signature", "set",
                    "collector_number", "card_id", "fixture_filename"])
        w.writerows(plan_rows)
    print(f"[plan] wrote {plan_path}")

    if args.dry_run:
        print("[dry-run] skipping downloads")
        return 0

    failures = []
    for i, (url, dst, sig, name) in enumerate(todo, 1):
        try:
            download_one(url, dst)
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as e:
            failures.append((dst, str(e)))
            print(f"  [FAIL] {sig} {name}: {e}")
        if i % 25 == 0 or i == len(todo):
            print(f"  [{i}/{len(todo)}] {sig} {name}")
        time.sleep(RATE_DELAY_SEC)

    print(f"\n[done] succeeded: {len(todo) - len(failures)}, "
          f"failed: {len(failures)}")
    if failures:
        print("[done] failed URLs can be re-tried on a rerun (idempotent).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
