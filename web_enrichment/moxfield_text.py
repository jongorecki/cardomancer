"""Parse and render the Moxfield plain-text decklist format.

Moxfield lets users paste/export decks as plain text.  Replaces the
auth'd push/pull flow (Cloudflare WAF blocks third-party clients).

Supported input line shapes:
    4 Lightning Bolt
    4x Lightning Bolt
    4 Lightning Bolt (LEA) 161
    4 Lightning Bolt (LEA)
    *F* 1 Lightning Bolt (LEA) 161      <-- foil marker (Moxfield export)
    1 Lightning Bolt *F*                 <-- alt foil marker
    // comment line
    <blank>                              <-- section separator
    Commander                            <-- section header, ignored
    Sideboard
    Maybeboard

We ignore section headers and foil markers when matching to Scryfall
(downstream consumers care about oracle_id only).
"""

from __future__ import annotations

import re
from typing import Iterable

# qty [name] optional "(SET)" optional collector number
# Leading `*F*` or trailing `*F*` foil markers are stripped before parsing.
_LINE_RE = re.compile(
    r"""^
        (?P<qty>\d+)x?      # 4  or 4x
        \s+
        (?P<name>.+?)       # card name (non-greedy)
        (?:\s+\((?P<set>[A-Za-z0-9]{2,6})\)   # optional (SET)
            (?:\s+(?P<cn>[A-Za-z0-9\-\u2605*]+))?   # CN only if set present
        )?
        \s*$
    """,
    re.VERBOSE,
)

_SECTION_HEADERS = {
    "deck", "commander", "companion", "sideboard", "maybeboard",
    "considering", "tokens", "signature spells", "attractions",
    "stickers", "contraptions",
}


def _strip_foil_markers(line: str) -> str:
    # Moxfield sometimes prefixes or suffixes "*F*" for foils.
    line = re.sub(r"^\s*\*F\*\s+", "", line)
    line = re.sub(r"\s+\*F\*\s*$", "", line)
    return line


def parse_text(text: str) -> list[dict]:
    """Parse a Moxfield plain-text decklist.

    Returns a list of {qty: int, name: str, set: str|None, collector_number: str|None}
    entries.  Section headers, comments, and unparseable lines are skipped silently
    (callers can surface them as warnings if they track `skipped_lines`).
    """
    out: list[dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue
        if line.lower().rstrip(":") in _SECTION_HEADERS:
            continue
        line = _strip_foil_markers(line)
        m = _LINE_RE.match(line)
        if not m:
            continue
        qty = int(m.group("qty"))
        if qty <= 0:
            continue
        out.append({
            "qty": qty,
            "name": m.group("name").strip(),
            "set": (m.group("set") or "").lower() or None,
            "collector_number": m.group("cn") or None,
        })
    return out


def resolve_entries(entries: Iterable[dict]) -> tuple[list[dict], list[str]]:
    """Resolve parsed entries against the local Scryfall bulk cache.

    Returns (resolved, unresolved_names):
      - resolved: list of {oracle_id, name, set_code, image_uri} dicts suitable
        for collection_db.upsert_moxfield_wishlist (dedup'd on oracle_id).
      - unresolved_names: names that had no English/paper printing match.

    Prefers the matching (set, collector_number) printing if provided, else
    the first English/paper printing of that name.
    """
    from cards import CARDS_DATA

    by_name: dict[str, list[dict]] = {}
    for c in CARDS_DATA:
        if c.get("lang") != "en" or "paper" not in (c.get("games") or []):
            continue
        name = (c.get("name") or "").strip().lower()
        if not name:
            continue
        by_name.setdefault(name, []).append(c)

    resolved: dict[str, dict] = {}
    unresolved: list[str] = []
    for e in entries:
        key = e["name"].strip().lower()
        candidates = by_name.get(key)
        if not candidates:
            unresolved.append(e["name"])
            continue

        picked = None
        if e.get("set"):
            set_key = e["set"].lower()
            cn_key = (e.get("collector_number") or "").lstrip("0").lower()
            for c in candidates:
                if (c.get("set") or "").lower() != set_key:
                    continue
                if cn_key and (c.get("collector_number") or "").lstrip("0").lower() != cn_key:
                    continue
                picked = c
                break
        if picked is None:
            picked = candidates[0]

        oracle_id = picked.get("oracle_id")
        if not oracle_id:
            unresolved.append(e["name"])
            continue

        image_uris = picked.get("image_uris") or {}
        if not image_uris:
            faces = picked.get("card_faces") or []
            if faces:
                image_uris = (faces[0] or {}).get("image_uris") or {}

        resolved[oracle_id] = {
            "oracle_id": oracle_id,
            "name": picked.get("name"),
            "set_code": picked.get("set"),
            "image_uri": image_uris.get("small") or image_uris.get("normal"),
        }

    return list(resolved.values()), unresolved


def render_text(rows: Iterable[dict]) -> str:
    """Render an iterable of inventory rows as Moxfield plain-text lines.

    Each row should have: name, set_code (optional), collector_number
    (optional), quantity, foil_quantity (optional).  Emits one line per
    non-zero quantity (non-foil and foil split so Moxfield distinguishes).
    """
    lines: list[str] = []
    for r in rows:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        set_code = (r.get("set_code") or "").upper()
        cn = (r.get("collector_number") or "").strip()
        suffix_parts = []
        if set_code:
            suffix_parts.append(f"({set_code})")
            if cn:
                suffix_parts.append(cn)
        suffix = (" " + " ".join(suffix_parts)) if suffix_parts else ""

        qty = int(r.get("quantity") or 0)
        foil_qty = int(r.get("foil_quantity") or 0)
        if qty > 0:
            lines.append(f"{qty} {name}{suffix}")
        if foil_qty > 0:
            lines.append(f"*F* {foil_qty} {name}{suffix}")
    return "\n".join(lines)
