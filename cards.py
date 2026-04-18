# cards.py
# Handles loading of card data, filtering, and extracting card information.
# Also applies rules for excluded sets, language, and paper availability.

import json
import os
from config import CARDS_JSON_PATH, EXCLUDED_SETS, PRINTINGS_MAP_PATH

# Load card data from JSON if available
if os.path.exists(CARDS_JSON_PATH):
    with open(CARDS_JSON_PATH, 'r', encoding='utf-8') as f:
        CARDS_DATA = json.load(f)
    CARD_DATA_BY_ID = {c.get('id'): c for c in CARDS_DATA}
else:
    CARDS_DATA = []
    CARD_DATA_BY_ID = {}

# Load printings map (illustration_id dedup data)
PRINTINGS_MAP = {}
if os.path.exists(PRINTINGS_MAP_PATH):
    with open(PRINTINGS_MAP_PATH, 'r', encoding='utf-8') as f:
        PRINTINGS_MAP = json.load(f)
    print(f"[cards] Loaded printings map: {len(PRINTINGS_MAP)} entries")
else:
    print(f"[cards] No printings map found at {PRINTINGS_MAP_PATH}")


def reload_card_data():
    """
    Re-read CARDS_DATA / CARD_DATA_BY_ID / PRINTINGS_MAP from disk
    IN PLACE, so every module that imported these containers at
    startup sees the fresh values without needing a process
    restart.

    This is the function the web "Refresh Prices" button calls
    after downloading a new Scryfall bulk data JSON. Since hash
    IDs are Scryfall card UUIDs (stable across bulk-data snapshots
    for any given printing), we can swap the JSON and get fresh
    prices / text / rulings without touching the hash DB.

    We mutate the existing `CARDS_DATA` list and `CARD_DATA_BY_ID`
    / `PRINTINGS_MAP` dicts rather than rebinding the names, so
    callers that captured a reference to the dict (e.g.
    `from cards import CARD_DATA_BY_ID` at module import time)
    keep working — their reference still points at the same
    container, which now holds the refreshed entries.

    Returns the new card count.
    """
    # Re-import config so any just-edited CARDS_JSON_PATH sticks.
    import importlib
    import config as _config_mod
    importlib.reload(_config_mod)
    cards_json_path = _config_mod.CARDS_JSON_PATH
    printings_map_path = _config_mod.PRINTINGS_MAP_PATH

    if os.path.exists(cards_json_path):
        with open(cards_json_path, 'r', encoding='utf-8') as f:
            new_cards = json.load(f)
        CARDS_DATA.clear()
        CARDS_DATA.extend(new_cards)
        CARD_DATA_BY_ID.clear()
        for c in new_cards:
            cid = c.get('id')
            if cid:
                CARD_DATA_BY_ID[cid] = c
        print(f"[cards] Reloaded {len(CARD_DATA_BY_ID)} cards "
              f"from {os.path.basename(cards_json_path)}")
    else:
        print(f"[cards] reload_card_data: {cards_json_path} not found")

    PRINTINGS_MAP.clear()
    if os.path.exists(printings_map_path):
        with open(printings_map_path, 'r', encoding='utf-8') as f:
            PRINTINGS_MAP.update(json.load(f))
        print(f"[cards] Reloaded printings map: "
              f"{len(PRINTINGS_MAP)} entries")

    return len(CARD_DATA_BY_ID)


# ---------------------------------------------------------------------------
# Price lookup with fallback chain
# ---------------------------------------------------------------------------
# Scryfall's bulk data has null USD prices for ~19% of English paper cards.
# This happens for foil-only printings, promos, and older sets.
# Fallback chain:
#   1. prices.usd (non-foil market price)
#   2. prices.usd_foil (foil market price — many promos are foil-only)
#   3. Cheapest price across all printings of the same card name
# ---------------------------------------------------------------------------

_name_price_index = None  # Lazy-init: {card_name_lower: cheapest_usd_float}


def _build_name_price_index():
    """Build a card-name -> cheapest USD price lookup from all loaded cards."""
    global _name_price_index
    _name_price_index = {}
    for c in CARDS_DATA:
        if 'paper' not in c.get('games', []):
            continue
        prices = c.get('prices', {})
        p = None
        for key in ('usd', 'usd_foil'):
            raw = prices.get(key)
            if raw:
                try:
                    p = float(raw)
                    break
                except (ValueError, TypeError):
                    pass
        if p is not None and p > 0:
            name_key = c.get('name', '').lower().strip()
            if name_key and (name_key not in _name_price_index
                             or p < _name_price_index[name_key]):
                _name_price_index[name_key] = p
    print(f"[cards] Built name-price index: {len(_name_price_index)} unique card names with prices")


def _get_price_str(card, name):
    """
    Extract USD price string from a card's Scryfall data with fallbacks.

    Returns "$X.XX" on success, "null" if no price found anywhere.
    """
    global _name_price_index
    prices = card.get('prices', {})

    # 1. Try non-foil USD price
    raw = prices.get('usd')
    if raw:
        try:
            return f"${float(raw):.2f}"
        except (ValueError, TypeError):
            pass

    # 2. Try foil USD price
    raw = prices.get('usd_foil')
    if raw:
        try:
            return f"${float(raw):.2f}"
        except (ValueError, TypeError):
            pass

    # 3. Try cheapest price from any printing of the same card name
    if _name_price_index is None:
        _build_name_price_index()

    name_key = name.lower().strip() if name else ''
    if name_key and name_key in _name_price_index:
        return f"${_name_price_index[name_key]:.2f}"

    return "null"


def extract_card_info(card_id):
    """
    Given a card_id, return a dictionary of card info including name, set,
    colors, CMC, type, price, and all sets this art appears in.
    If the card is not found, return None.
    """
    card = CARD_DATA_BY_ID.get(card_id)
    if not card:
        return None

    name = card.get('name', 'Unknown')
    set_code = card.get('set', '???')
    colors = card.get('colors', [])
    color_identity = card.get('color_identity', [])
    cmc = card.get('cmc', None)
    price_str = _get_price_str(card, name)

    type_line = card.get('type_line', '').lower()
    possible_types = ["creature", "artifact", "enchantment", "instant", "sorcery", "battle", "planeswalker", "land"]
    found_types = [t for t in possible_types if t in type_line]

    # Get all sets this art appears in from printings map
    all_sets = [set_code]
    printings_entry = PRINTINGS_MAP.get(card_id)
    if printings_entry:
        all_sets = list(dict.fromkeys(
            p['set'] for p in printings_entry.get('printings', [])
            if p.get('set')
        ))

    info = {
        "Name": name,
        "Set": set_code,
        "Sets": all_sets,
        "Colors": colors,
        "Color Identity": color_identity,
        "CMC": cmc,
        "Types": found_types,
        "Price": price_str,
        "Rarity": card.get('rarity', ''),
    }

    return info


def card_is_allowed(card_id):
    """
    Returns True if the card meets criteria for allowed sets, games, and language.
    For example, we only consider cards that have 'paper' in their games attribute.
    We also exclude certain sets as primary matches.
    """
    c = CARD_DATA_BY_ID.get(card_id)
    if not c:
        return False

    set_code = c.get('set', '').lower()
    games = c.get('games', [])
    lang = c.get('lang', '')

    # Must be playable in paper
    if 'paper' not in games:
        return False

    # If set_code is in excluded sets, we do not consider it as primary allowed match
    if set_code in EXCLUDED_SETS:
        return False

    # Additional criteria can be added here, e.g. language checks if needed.

    return True


def get_illustration_id(card_id):
    """
    Returns the illustration_id of the given card_id, or None if not found.
    """
    c = CARD_DATA_BY_ID.get(card_id)
    if c:
        return c.get('illustration_id')
    return None


def get_same_illustration_english_candidates(illustration_id):
    """
    Given an illustration_id, return a list of (card_id, set_code, distance) candidates
    that share the same illustration_id, are in English, allowed sets, and paper format.
    The distance is not known here; this function can be adjusted to only return card_ids.
    In practice, you'd call this after you've computed distances to filter for the best candidate.
    """
    candidates = []
    for c in CARDS_DATA:
        if c.get('illustration_id') == illustration_id and c.get('lang') == 'en':
            set_code = c.get('set', '').lower()
            games = c.get('games', [])
            if 'paper' in games and set_code not in EXCLUDED_SETS:
                cid = c.get('id')
                candidates.append(cid)
    return candidates
