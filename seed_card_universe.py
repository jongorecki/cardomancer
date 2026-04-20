# seed_card_universe.py
# ---------------------------------------------------------------------------
# One-shot script: seed card_universe from the most recent Scryfall bulk
# default-cards JSON. Safe to re-run (upserts on oracle_id).
# ---------------------------------------------------------------------------

from __future__ import annotations

import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import enrichment_db


def find_bulk_file() -> str:
    pattern = os.path.join(os.path.dirname(__file__), "default-cards-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        raise FileNotFoundError(f"No default-cards-*.json found in {os.path.dirname(__file__)}")
    return files[-1]


def seed(bulk_path: str) -> int:
    print(f"Loading {os.path.basename(bulk_path)} ...")
    t0 = time.time()
    with open(bulk_path, encoding="utf-8") as f:
        cards = json.load(f)

    seen: dict[str, str] = {}
    for c in cards:
        if c.get("lang") != "en":
            continue
        oid = c.get("oracle_id")
        name = c.get("name")
        if oid and name:
            seen[oid] = name

    print(f"  {len(seen)} unique oracle_ids found in {time.time()-t0:.1f}s")

    conn = enrichment_db.get_connection()
    try:
        n = enrichment_db.seed_card_universe(conn, list(seen.items()))
        print(f"  {n} rows upserted into card_universe")
    finally:
        conn.close()
    return n


if __name__ == "__main__":
    path = find_bulk_file()
    seed(path)
    print("Done.")
