# preset_store.py
# ---------------------------------------------------------------------------
# Unified sort-preset catalog for the Sort Session tab (Phase 0B-1).
#
# Three preset sources, exposed under one shape:
#   * builtin — color / mana_value / set / price / type. Read-only; the
#               existing hardcoded bin-assignment logic in sorting.py
#               keeps running at sort time. The query strings here are
#               for display only, so the UI can render a uniform table.
#   * file    — sort_configs/*.txt. Read-only; auto-imported on startup.
#   * user    — user_presets/*.json. Fully editable.
#
# A preset is a dict:
#   {
#     "id": str,                # "builtin:color" | "file:<name>" | "user:<slug>"
#     "name": str,              # display name
#     "source": "builtin" | "file" | "user",
#     "editable": bool,
#     "description": str,
#     "bin_count": int,
#     "fallback_bin": int,
#     "bin_limit": int | None,
#     "bins": [
#       {"bin": int, "query": str, "description": str},
#       ...
#     ],
#   }
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from config import SCRIPT_DIR, SORT_CONFIGS_DIR

USER_PRESETS_DIR = Path(SCRIPT_DIR) / "user_presets"


# ---------------------------------------------------------------------------
# Builtins
# ---------------------------------------------------------------------------

# Display-only queries that mirror sorting.py's hardcoded logic. These are
# not actually evaluated at sort time — the legacy get_bin_number(...) path
# is used — but they let the uniform preset table describe what each bin
# holds.
_BUILTIN_DEFS: list[dict[str, Any]] = [
    {
        "id": "builtin:color",
        "name": "Color",
        "description": "WUBRG + Colorless / Multi / Lands (legacy color sort).",
        "bin_count": 10,
        "fallback_bin": 10,
        "bins": [
            {"bin": 1, "query": "c=w -t:land", "description": "White (non-land)"},
            {"bin": 2, "query": "c=u -t:land", "description": "Blue (non-land)"},
            {"bin": 3, "query": "c=b -t:land", "description": "Black (non-land)"},
            {"bin": 4, "query": "c=r -t:land", "description": "Red (non-land)"},
            {"bin": 5, "query": "c=g -t:land", "description": "Green (non-land)"},
            {"bin": 6, "query": "c=c -t:land", "description": "Colorless (non-land)"},
            {"bin": 7, "query": "c:m -t:land", "description": "Multicolor"},
            {"bin": 8, "query": "t:land -is:basic", "description": "Nonbasic lands"},
            {"bin": 9, "query": "is:basic", "description": "Basic lands"},
            {"bin": 10, "query": "*", "description": "Errors / unmatched"},
        ],
    },
    {
        "id": "builtin:mana_value",
        "name": "Mana Value",
        "description": "Buckets by converted mana cost (1 … 7, 8+).",
        "bin_count": 10,
        "fallback_bin": 10,
        "bins": [
            {"bin": i, "query": f"cmc={i}", "description": f"CMC {i}"}
            for i in range(1, 8)
        ] + [
            {"bin": 8, "query": "cmc>=8", "description": "CMC 8+"},
            {"bin": 10, "query": "*", "description": "Errors / unmatched"},
        ],
    },
    {
        "id": "builtin:set",
        "name": "Set",
        "description": "Bin-per-set assignments (configurable in the Set panel).",
        "bin_count": 10,
        "fallback_bin": 9,
        "bins": [
            {"bin": 1, "query": "s:<assigned>", "description": "Set group 1"},
            {"bin": 2, "query": "s:<assigned>", "description": "Set group 2"},
            {"bin": 9, "query": "*", "description": "Unknown sets"},
            {"bin": 10, "query": "*", "description": "Errors"},
        ],
    },
    {
        "id": "builtin:price",
        "name": "Price Tiers",
        "description": "Under $0.50 / $0.50–1 / $1–5 / $5–10 / $10+.",
        "bin_count": 10,
        "fallback_bin": 10,
        "bins": [
            {"bin": 1, "query": "usd<0.5", "description": "< $0.50"},
            {"bin": 2, "query": "usd>=0.5 usd<1", "description": "$0.50 – $1"},
            {"bin": 3, "query": "usd>=1 usd<5", "description": "$1 – $5"},
            {"bin": 4, "query": "usd>=5 usd<10", "description": "$5 – $10"},
            {"bin": 5, "query": "usd>=10", "description": "$10+"},
            {"bin": 10, "query": "*", "description": "Price unknown / errors"},
        ],
    },
    {
        "id": "builtin:type",
        "name": "Card Type",
        "description": "Creature / artifact / enchantment / instant / sorcery / battle / planeswalker / land.",
        "bin_count": 10,
        "fallback_bin": 9,
        "bins": [
            {"bin": 1, "query": "t:creature", "description": "Creature"},
            {"bin": 2, "query": "t:artifact -t:creature", "description": "Artifact"},
            {"bin": 3, "query": "t:enchantment -t:creature", "description": "Enchantment"},
            {"bin": 4, "query": "t:instant", "description": "Instant"},
            {"bin": 5, "query": "t:sorcery", "description": "Sorcery"},
            {"bin": 6, "query": "t:battle", "description": "Battle"},
            {"bin": 7, "query": "t:planeswalker", "description": "Planeswalker"},
            {"bin": 8, "query": "t:land", "description": "Land"},
            {"bin": 9, "query": "*", "description": "Unknown"},
            {"bin": 10, "query": "*", "description": "Errors"},
        ],
    },
]

# Legacy single-word preset names → canonical ids (for back-compat resolvers).
LEGACY_NAME_MAP = {
    "color": "builtin:color",
    "mana_value": "builtin:mana_value",
    "cmc": "builtin:mana_value",
    "set": "builtin:set",
    "price": "builtin:price",
    "type": "builtin:type",
}


def _builtin(name_id: str) -> dict[str, Any]:
    for d in _BUILTIN_DEFS:
        if d["id"] == name_id:
            return _finalize(d, source="builtin", editable=False)
    raise KeyError(name_id)


def list_builtin_presets() -> list[dict[str, Any]]:
    return [_finalize(d, source="builtin", editable=False) for d in _BUILTIN_DEFS]


# ---------------------------------------------------------------------------
# File-based (sort_configs/*.txt)
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower())
    return s.strip("_") or "preset"


def list_file_presets(configs_dir: Optional[str] = None) -> list[dict[str, Any]]:
    """Read every sort_configs/*.txt file and emit a read-only preset."""
    base = Path(configs_dir or SORT_CONFIGS_DIR)
    if not base.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(base.glob("*.txt")):
        try:
            preset = _parse_file_preset(p)
        except Exception as e:
            # Malformed file → surface as a read-only preset with no bins so
            # the user sees it in the UI and can fix the underlying file.
            preset = {
                "id": f"file:{p.stem}",
                "name": p.stem,
                "description": f"(parse error: {e})",
                "bin_count": 0,
                "fallback_bin": 0,
                "bin_limit": None,
                "bins": [],
            }
        out.append(_finalize(preset, source="file", editable=False))
    return out


def _parse_file_preset(path: Path) -> dict[str, Any]:
    """Best-effort parse of the sort_config.py line format, without
    importing sort_config (which pulls heavy runtime deps)."""
    bins: list[dict[str, Any]] = []
    bin_count = 0
    fallback = 0
    bin_limit: Optional[int] = None
    desc_lines: list[str] = []

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            desc_lines.append(line.lstrip("# ").strip())
            continue
        low = line.lower()
        if low.startswith("bins:"):
            try:
                bin_count = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            continue
        if low.startswith("fallback:"):
            try:
                fallback = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            continue
        if low.startswith("limit:"):
            try:
                bin_limit = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            continue
        m = re.match(r"^bin(\d+)\s*:\s*(.+)$", line, re.IGNORECASE)
        if m:
            bins.append({
                "bin": int(m.group(1)),
                "query": m.group(2).strip(),
                "description": "",
            })

    return {
        "id": f"file:{path.stem}",
        "name": path.stem,
        "description": " ".join(desc_lines).strip() or f"Loaded from {path.name}.",
        "bin_count": bin_count or max((b["bin"] for b in bins), default=0),
        "fallback_bin": fallback,
        "bin_limit": bin_limit,
        "bins": bins,
    }


# ---------------------------------------------------------------------------
# User-saved presets (user_presets/*.json)
# ---------------------------------------------------------------------------

def _user_dir(override: Optional[str] = None) -> Path:
    d = Path(override) if override else USER_PRESETS_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_user_presets(user_dir: Optional[str] = None) -> list[dict[str, Any]]:
    d = _user_dir(user_dir)
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        raw.setdefault("id", f"user:{p.stem}")
        raw.setdefault("name", p.stem)
        raw.setdefault("description", "")
        raw.setdefault("bin_count", max((b.get("bin", 0) for b in raw.get("bins", [])), default=0))
        raw.setdefault("fallback_bin", raw["bin_count"])
        raw.setdefault("bin_limit", None)
        raw.setdefault("bins", [])
        out.append(_finalize(raw, source="user", editable=True))
    return out


def save_user_preset(preset: dict[str, Any], user_dir: Optional[str] = None) -> dict[str, Any]:
    """Save / overwrite a user preset. `preset["name"]` is slugified into
    the filename. Returns the normalized preset dict (with final id)."""
    d = _user_dir(user_dir)
    name = preset.get("name") or "preset"
    slug = _slugify(name)
    path = d / f"{slug}.json"
    stored = {
        "id": f"user:{slug}",
        "name": name,
        "description": preset.get("description", ""),
        "bin_count": int(preset.get("bin_count")
                         or max((b.get("bin", 0) for b in preset.get("bins", [])),
                                default=0)),
        "fallback_bin": int(preset.get("fallback_bin") or 0),
        "bin_limit": preset.get("bin_limit"),
        "bins": [
            {
                "bin": int(b["bin"]),
                "query": str(b.get("query", "")),
                "description": str(b.get("description", "")),
            }
            for b in preset.get("bins", [])
        ],
    }
    path.write_text(json.dumps(stored, indent=2), encoding="utf-8")
    return _finalize(stored, source="user", editable=True)


def delete_user_preset(preset_id: str, user_dir: Optional[str] = None) -> bool:
    if not preset_id.startswith("user:"):
        return False
    slug = preset_id.split(":", 1)[1]
    if not re.match(r"^[a-z0-9_\-]+$", slug):
        return False
    d = _user_dir(user_dir)
    path = d / f"{slug}.json"
    if path.exists():
        path.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Catalog + resolution
# ---------------------------------------------------------------------------

def list_presets(*, configs_dir: Optional[str] = None,
                 user_dir: Optional[str] = None) -> list[dict[str, Any]]:
    return (
        list_builtin_presets()
        + list_file_presets(configs_dir)
        + list_user_presets(user_dir)
    )


def resolve_preset(preset_id: str, *, configs_dir: Optional[str] = None,
                   user_dir: Optional[str] = None) -> Optional[dict[str, Any]]:
    # Legacy shortcut names
    if preset_id in LEGACY_NAME_MAP:
        preset_id = LEGACY_NAME_MAP[preset_id]

    for p in list_presets(configs_dir=configs_dir, user_dir=user_dir):
        if p["id"] == preset_id:
            return p
    return None


# ---------------------------------------------------------------------------
# Per-bin count estimation
# ---------------------------------------------------------------------------

def estimate_bin_counts(preset: dict[str, Any], collection_conn) -> dict[int, int]:
    """Estimate card counts per bin by evaluating each bin query against
    collection.db. Builtin presets with placeholder queries (``*`` or
    ``s:<assigned>``) are counted as 0 and the caller should fall back
    to the mode-specific bin function for display.

    Accepts any collection_db connection (sqlite3.Connection). Safe to
    pass None — returns an empty dict.
    """
    if collection_conn is None:
        return {}
    try:
        from query_parser import parse_query, evaluate_query
    except ImportError:
        return {}

    total = _collection_rows(collection_conn)
    if not total:
        return {b["bin"]: 0 for b in preset.get("bins", [])}

    counts: dict[int, int] = {}
    for entry in preset.get("bins", []):
        q = entry.get("query", "").strip()
        if not q or q in ("*", "s:<assigned>"):
            counts[entry["bin"]] = 0
            continue
        try:
            ast = parse_query(q)
        except Exception:
            counts[entry["bin"]] = 0
            continue
        c = 0
        for row in total:
            try:
                if evaluate_query(ast, row, {}):
                    c += 1
            except Exception:
                continue
        counts[entry["bin"]] = c
    return counts


def _collection_rows(conn) -> list[dict[str, Any]]:
    """Best-effort adapter from collection.db rows to the dict shape
    query_parser.evaluate_query expects."""
    try:
        cur = conn.execute("SELECT * FROM inventory LIMIT 10000")
    except Exception:
        return []
    cols = [c[0] for c in cur.description]
    rows = []
    for r in cur.fetchall():
        d = dict(zip(cols, r))
        # evaluate_query expects specific shaped keys; provide best-effort
        # aliases without mutating the DB.
        d.setdefault("Name", d.get("name"))
        d.setdefault("Colors", _as_list(d.get("colors") or d.get("color_identity")))
        d.setdefault("CMC", d.get("cmc") or 0)
        d.setdefault("Types", _as_list(d.get("type_line") or d.get("types")))
        d.setdefault("Set", d.get("set_code") or d.get("set"))
        d.setdefault("Price", d.get("price_usd") or d.get("usd"))
        rows.append(d)
    return rows


def _as_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        if v.startswith("[") and v.endswith("]"):
            try:
                return json.loads(v)
            except Exception:
                pass
        return [t.strip() for t in re.split(r"[\s,]+", v) if t.strip()]
    return [v]


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _finalize(preset: dict[str, Any], *, source: str, editable: bool) -> dict[str, Any]:
    out = dict(preset)
    out["source"] = source
    out["editable"] = editable
    out.setdefault("bin_limit", None)
    # Sort bins by bin number for stable display.
    out["bins"] = sorted(
        (dict(b) for b in out.get("bins", [])),
        key=lambda b: int(b.get("bin", 0)),
    )
    return out
