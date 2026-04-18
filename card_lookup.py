# card_lookup.py
# Card lookup by name and set/collector number using Scryfall bulk data.

import json
import os
import re
import difflib

from config import CARDS_JSON_PATH


def _normalize_name(name):
    """Normalize a card name for matching: lowercase, strip punctuation."""
    name = name.lower().strip()
    # Remove common punctuation but keep spaces and hyphens
    name = re.sub(r"[^a-z0-9 \-]", "", name)
    # Collapse multiple spaces
    name = re.sub(r"\s+", " ", name)
    return name


# --- Load and index card data ---
print("[card_lookup] Loading card database...")

_ALL_CARDS = []
_BY_NAME = {}           # { normalized_name: [card_dict, ...] }
_BY_SET_COLLECTOR = {}  # { (set_code, collector_number): card_dict }
ALL_NAMES = []          # original card names for fuzzy matching

if os.path.exists(CARDS_JSON_PATH):
    with open(CARDS_JSON_PATH, 'r', encoding='utf-8') as f:
        _ALL_CARDS = json.load(f)

    for card in _ALL_CARDS:
        name = card.get('name', '')
        normalized = _normalize_name(name) if name else ''

        if normalized:
            if normalized not in _BY_NAME:
                _BY_NAME[normalized] = []
                ALL_NAMES.append(name)
            _BY_NAME[normalized].append(card)

        set_code = card.get('set', '').lower()
        collector = str(card.get('collector_number', ''))
        if set_code and collector:
            _BY_SET_COLLECTOR[(set_code, collector)] = card

    print(f"[card_lookup] Loaded {len(_ALL_CARDS)} cards, "
          f"{len(ALL_NAMES)} unique names, "
          f"{len(_BY_SET_COLLECTOR)} set/collector entries.")
else:
    print(f"[card_lookup] WARNING: Card database not found at {CARDS_JSON_PATH}")


def lookup_by_set_collector(set_code, collector_number):
    """
    Exact lookup by set code + collector number.
    Returns the card dict or None.
    """
    if not set_code or not collector_number:
        return None
    key = (set_code.lower(), str(collector_number))
    return _BY_SET_COLLECTOR.get(key)


def lookup_by_name(ocr_text, cutoff=0.6):
    """
    Look up a card by OCR'd name text.
    Tries exact match first, then fuzzy matching.
    Returns (card_name, card_list) or (None, None).
    """
    if not ocr_text:
        return None, None

    normalized = _normalize_name(ocr_text)

    # Exact match
    if normalized in _BY_NAME:
        cards = _BY_NAME[normalized]
        return cards[0].get('name'), cards

    # Fuzzy match against all known names
    matches = difflib.get_close_matches(
        ocr_text, ALL_NAMES, n=5, cutoff=cutoff
    )
    if matches:
        best_name = matches[0]
        best_normalized = _normalize_name(best_name)
        cards = _BY_NAME.get(best_normalized, [])
        return best_name, cards

    return None, None


def get_card_info_from_data(card):
    """
    Convert a Scryfall card dict to the info format used by sorting.py.
    """
    if not card:
        return None

    name = card.get('name', 'Unknown')
    set_code = card.get('set', '???')
    colors = card.get('colors', [])
    color_identity = card.get('color_identity', [])
    cmc = card.get('cmc', 0)

    usd_price = card.get('prices', {}).get('usd') if isinstance(card.get('prices'), dict) else None
    price_str = "null"
    if usd_price:
        try:
            price_str = f"${float(usd_price):.2f}"
        except (ValueError, TypeError):
            pass

    type_line = card.get('type_line', '').lower()
    possible_types = ["creature", "artifact", "enchantment", "instant",
                      "sorcery", "battle", "planeswalker", "land"]
    found_types = [t for t in possible_types if t in type_line]

    return {
        "Name": name,
        "Set": set_code,
        "Sets": [set_code],
        "Colors": colors,
        "Color Identity": color_identity,
        "CMC": cmc,
        "Types": found_types,
        "Price": price_str,
    }
