"""
Post-sort session CSV corrector using Haiku vision.

Two passes:
  1. Identity check  — cards with hash_distance > IDENTITY_THRESHOLD (default 75)
     Sends card_crops/card_XXXX.jpg to Haiku and asks for card name + set + CN.
     If the answer disagrees with the CSV name, the row is flagged and corrected.

  2. Foil check — cards with |foil_confidence| < FOIL_THRESHOLD (default 0.5)
     Sends card_crops_b/card_XXXX.jpg (foil-lighting capture) to Haiku.
     If Haiku's foil/nonfoil answer disagrees with is_foil, the row is corrected.

When identity is corrected the frame/border_color/frame_effects columns are also
re-resolved from the local Scryfall data for the newly identified printing.

Usage:
    python correct_session_csv.py <session_dir> [--identity-threshold 75]
                                                [--foil-threshold 0.5]
                                                [--dry-run]

Writes:
    scans.csv          — corrected in-place (original backed up to scans_precorrect.csv)
    corrections_report.txt — every change made with before/after values
"""

import argparse
import base64
import csv
import glob
import json
import os
import sys
import time
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import anthropic

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL = "claude-haiku-4-5-20251001"

CSV_COLS = [
    "scan_num", "timestamp", "name", "set", "all_sets",
    "collector_number", "colors", "cmc", "type_line", "rarity",
    "price_usd", "bin", "method", "hash_distance", "recognized",
    "is_foil", "foil_confidence", "frame", "border_color", "frame_effects",
]


# ---------------------------------------------------------------------------
# Scryfall index
# ---------------------------------------------------------------------------

def load_scryfall_index():
    pattern = os.path.join(SCRIPT_DIR, "default-cards-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError("No default-cards-*.json found")
    with open(files[-1], "r", encoding="utf-8") as f:
        cards = json.load(f)
    by_id = {}        # (set, cn) -> card
    by_name = {}      # lower_name -> [card, ...]
    for c in cards:
        key = (c.get("set", "").lower(), str(c.get("collector_number", "")))
        by_id[key] = c
        name_key = c.get("name", "").lower()
        by_name.setdefault(name_key, []).append(c)
    return by_id, by_name


def scryfall_meta(card):
    """Extract frame/border/frame_effects from a Scryfall card record."""
    fe = card.get("frame_effects") or []
    return card.get("frame", ""), card.get("border_color", ""), ";".join(fe)


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def encode_image(path):
    with open(path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def crop_path(session_dir, scan_num, side="a"):
    folder = "card_crops" if side == "a" else "card_crops_b"
    return os.path.join(session_dir, folder, f"card_{int(scan_num):04d}.jpg")


# ---------------------------------------------------------------------------
# Haiku calls
# ---------------------------------------------------------------------------

def ask_identity(client, image_b64):
    """Ask Haiku to identify the card. Returns (name, set_code, cn) strings."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=64,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "This is a Magic: The Gathering card. "
                        "Reply with exactly: NAME | SET_CODE | COLLECTOR_NUMBER\n"
                        "Use the Scryfall set code (3-5 letters). "
                        "If you cannot read the collector number clearly, use '?'. "
                        "No other text."
                    ),
                },
            ],
        }],
    )
    text = resp.content[0].text.strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) == 3:
        return parts[0], parts[1].lower(), parts[2]
    # Fallback: just name
    return text, "", "?"


def ask_foil(client, image_b64):
    """Ask Haiku if the card is foil. Returns True/False or None if uncertain."""
    resp = client.messages.create(
        model=MODEL,
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64},
                },
                {
                    "type": "text",
                    "text": (
                        "Is this Magic: The Gathering card foil? "
                        "Foil cards have a holographic/rainbow sheen on the card face. "
                        "Reply with exactly one word: foil or nonfoil"
                    ),
                },
            ],
        }],
    )
    text = resp.content[0].text.strip().lower()
    if "nonfoil" in text or "non-foil" in text or "non foil" in text:
        return False
    if "foil" in text:
        return True
    return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(session_dir, identity_threshold, foil_threshold, dry_run):
    csv_path = os.path.join(session_dir, "scans.csv")
    report_path = os.path.join(session_dir, "corrections_report.txt")

    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    scryfall_by_id, scryfall_by_name = load_scryfall_index()

    client = anthropic.Anthropic()
    corrections = []
    total_api_calls = 0

    # --- Pass 1: identity check ---
    identity_candidates = [
        r for r in rows
        if r.get("hash_distance") and r.get("recognized") == "True"
        and float(r["hash_distance"]) > identity_threshold
    ]
    print(f"\nPass 1 — identity check: {len(identity_candidates)} cards (hash_distance > {identity_threshold})")

    for r in identity_candidates:
        img_path = crop_path(session_dir, r["scan_num"], "a")
        if not os.path.exists(img_path):
            print(f"  #{r['scan_num']} SKIP — no crop image")
            continue

        img_b64 = encode_image(img_path)
        ai_name, ai_set, ai_cn = ask_identity(client, img_b64)
        total_api_calls += 1

        csv_name = r["name"].lower().strip()
        ai_name_lower = ai_name.lower().strip()

        # Consider it a mismatch if the names differ meaningfully
        name_match = (csv_name == ai_name_lower or
                      csv_name in ai_name_lower or
                      ai_name_lower in csv_name)

        status = "OK" if name_match else "MISMATCH"
        marker = "  " if name_match else "* "
        print(f"{marker}#{int(r['scan_num']):4d} dist={float(r['hash_distance']):.1f}  "
              f"csv='{r['name']}'  ai='{ai_name}' [{status}]")

        if not name_match:
            change = {
                "scan_num": r["scan_num"],
                "type": "identity",
                "field": "name",
                "old": r["name"],
                "new": ai_name,
                "detail": f"AI also guessed set={ai_set} cn={ai_cn}",
            }
            corrections.append(change)

            if not dry_run:
                r["name"] = ai_name
                # Try to resolve the new name against Scryfall
                candidates = scryfall_by_name.get(ai_name_lower, [])
                if not candidates and ai_set and ai_cn != "?":
                    card = scryfall_by_id.get((ai_set, ai_cn))
                    if card:
                        candidates = [card]
                if candidates:
                    # Prefer the printing that matches the AI-suggested set/cn
                    matched = next(
                        (c for c in candidates
                         if c.get("set", "").lower() == ai_set and
                            str(c.get("collector_number", "")) == ai_cn),
                        candidates[0]
                    )
                    r["set"] = matched.get("set", r["set"])
                    r["collector_number"] = matched.get("collector_number", r["collector_number"])
                    r["frame"], r["border_color"], r["frame_effects"] = scryfall_meta(matched)
                    change["detail"] += f"  → resolved to {r['set']}/{r['collector_number']}"

        # Small delay to avoid rate limiting
        time.sleep(0.1)

    # --- Pass 2: foil check ---
    foil_candidates = [
        r for r in rows
        if r.get("foil_confidence") and r.get("recognized") == "True"
        and abs(float(r["foil_confidence"])) < foil_threshold
    ]
    print(f"\nPass 2 — foil check: {len(foil_candidates)} cards (|foil_confidence| < {foil_threshold})")

    for r in foil_candidates:
        img_path = crop_path(session_dir, r["scan_num"], "b")
        if not os.path.exists(img_path):
            img_path = crop_path(session_dir, r["scan_num"], "a")
        if not os.path.exists(img_path):
            print(f"  #{r['scan_num']} SKIP — no crop image")
            continue

        img_b64 = encode_image(img_path)
        ai_foil = ask_foil(client, img_b64)
        total_api_calls += 1

        if ai_foil is None:
            print(f"  #{r['scan_num']:4d} conf={float(r['foil_confidence']):.3f}  UNCERTAIN (AI couldn't tell)")
            continue

        csv_foil = r["is_foil"] in ("1", "True", "true")
        match = (csv_foil == ai_foil)
        marker = "  " if match else "* "
        print(f"{marker}#{int(r['scan_num']):4d} conf={float(r['foil_confidence']):.3f}  "
              f"csv={'foil' if csv_foil else 'nonfoil'}  "
              f"ai={'foil' if ai_foil else 'nonfoil'} "
              f"{'OK' if match else 'MISMATCH'}")

        if not match:
            corrections.append({
                "scan_num": r["scan_num"],
                "type": "foil",
                "field": "is_foil",
                "old": r["is_foil"],
                "new": "1" if ai_foil else "0",
                "detail": f"foil_confidence was {r['foil_confidence']}",
            })
            if not dry_run:
                r["is_foil"] = "1" if ai_foil else "0"

        time.sleep(0.1)

    # --- Write corrected CSV ---
    if not dry_run and corrections:
        backup = os.path.join(session_dir, "scans_precorrect.csv")
        import shutil
        shutil.copy(csv_path, backup)
        print(f"\nOriginal backed up to scans_precorrect.csv")

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        print(f"Corrected CSV written to scans.csv")

    # --- Write report ---
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"Session: {session_dir}\n")
        f.write(f"Total API calls: {total_api_calls}\n")
        f.write(f"Corrections made: {len(corrections)}\n")
        f.write(f"Dry run: {dry_run}\n\n")

        identity_fixes = [c for c in corrections if c["type"] == "identity"]
        foil_fixes = [c for c in corrections if c["type"] == "foil"]

        f.write(f"--- Identity corrections ({len(identity_fixes)}) ---\n")
        for c in identity_fixes:
            f.write(f"  Scan #{c['scan_num']}: '{c['old']}' -> '{c['new']}'\n")
            if c["detail"]:
                f.write(f"    {c['detail']}\n")

        f.write(f"\n--- Foil corrections ({len(foil_fixes)}) ---\n")
        for c in foil_fixes:
            f.write(f"  Scan #{c['scan_num']}: is_foil {c['old']} -> {c['new']}  ({c['detail']})\n")

    print(f"\n{'DRY RUN — ' if dry_run else ''}Done.")
    print(f"  Identity mismatches: {len(identity_fixes)}")
    print(f"  Foil corrections:    {len(foil_fixes)}")
    print(f"  Total API calls:     {total_api_calls}")
    print(f"  Report:              {report_path}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_dir", help="Path to session directory")
    parser.add_argument("--identity-threshold", type=float, default=75.0,
                        help="hash_distance above which to run identity check (default: 75)")
    parser.add_argument("--foil-threshold", type=float, default=0.5,
                        help="|foil_confidence| below which to run foil check (default: 0.5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would change without modifying the CSV")
    args = parser.parse_args()
    run(args.session_dir, args.identity_threshold, args.foil_threshold, args.dry_run)
