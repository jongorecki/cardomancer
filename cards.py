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

    # Invalidate lazy price indexes so they rebuild from fresh data
    global _name_price_index, _art_min_price_index, _cheapest_printing_index
    _name_price_index = None
    _art_min_price_index = None
    _cheapest_printing_index = None

    return len(CARD_DATA_BY_ID)


# ---------------------------------------------------------------------------
# Price lookup with fallback chain
# ---------------------------------------------------------------------------
# Scryfall's bulk data has null USD prices for ~19% of English paper cards.
# This happens for foil-only printings, promos, and older sets.
# Fallback chain:
#   0. Art-min-price: cheapest nonfoil across all same-art printings
#      (prevents promo/expensive printing from inflating sort price)
#   1. prices.usd (non-foil market price)        — for cards not in printings map
#   2. prices.usd_foil (foil market price)        — foil-only printings
#   3. Cheapest price across all printings of the same card name
# ---------------------------------------------------------------------------

_name_price_index = None         # Lazy-init: {card_name_lower: cheapest_usd_float}
_art_min_price_index = None      # Lazy-init: {card_id: cheapest_nonfoil_usd_float across same-art printings}
_cheapest_printing_index = None  # Lazy-init: {card_id: cheapest_sibling_card_id across same-art printings}


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


def _build_art_min_price_index():
    """
    Build card_id -> min nonfoil USD price across all same-art printings,
    AND card_id -> cheapest-sibling card_id for the same groupings.

    Iterates every illustration group in PRINTINGS_MAP, finds the cheapest
    nonfoil printing (both price AND its card_id), and maps every card ID
    in the group to both values.  Cards with no art group (unique printings)
    are not added; callers fall back to the card's own price / id.
    """
    global _art_min_price_index, _cheapest_printing_index
    _art_min_price_index = {}
    _cheapest_printing_index = {}

    for rep_id, entry in PRINTINGS_MAP.items():
        printings = entry.get('printings', [])
        # Collect all card IDs in this illustration group (rep + all printings)
        all_ids = [rep_id] + [p['id'] for p in printings if p.get('id')]

        # Find cheapest nonfoil printing across the group (price + card_id)
        min_price = None
        cheapest_id = None
        for cid in all_ids:
            c = CARD_DATA_BY_ID.get(cid)
            if not c:
                continue
            raw = (c.get('prices') or {}).get('usd')
            if raw:
                try:
                    p = float(raw)
                    if p > 0 and (min_price is None or p < min_price):
                        min_price = p
                        cheapest_id = cid
                except (ValueError, TypeError):
                    pass

        if min_price is not None and cheapest_id is not None:
            for cid in all_ids:
                _art_min_price_index[cid] = min_price
                _cheapest_printing_index[cid] = cheapest_id

    print(f"[cards] Built art-min-price index: {len(_art_min_price_index)} card IDs")


def get_art_min_price(card_id):
    """
    Return the minimum nonfoil USD price across all same-art printings for
    the given card ID, or None if this card has no art group or no prices.
    """
    global _art_min_price_index
    if _art_min_price_index is None:
        _build_art_min_price_index()
    return _art_min_price_index.get(card_id)


def get_cheapest_printing_id(card_id):
    """
    Return the card_id of the cheapest nonfoil printing sharing this card's
    art (illustration group).  Used to remap identified serialized / promo
    printings down to the regular cheap printing so sort decisions reflect
    the card's "real" value, not the serialized stamp's market price.

    Returns the input card_id unchanged if:
      - card_id is None / unknown
      - card has no art group (unique printing)
      - no printing in the group has a valid nonfoil USD price

    Note: siblings share illustration_id (same art).  Serialized MUL printings
    DO share art with their normal counterparts in Scryfall's data, so this
    remap catches the $285 serialized -> $0.50 normal case.
    """
    global _cheapest_printing_index
    if _cheapest_printing_index is None:
        _build_art_min_price_index()
    if not card_id:
        return card_id
    return _cheapest_printing_index.get(card_id, card_id)


def _get_price_str(card, name):
    """
    Extract USD price string from a card's Scryfall data with fallbacks.

    Returns "$X.XX" on success, "null" if no price found anywhere.
    """
    global _name_price_index

    # 0. Art-min-price: cheapest nonfoil across all same-art printings.
    #    This prevents a promo/expensive printing from inflating the sort price
    #    (e.g. a $10 promo Farseek should sort alongside 50-cent copies).
    card_id = card.get('id')
    if card_id:
        art_price = get_art_min_price(card_id)
        if art_price is not None:
            return f"${art_price:.2f}"

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
        "Id": card_id,
        "CollectorNumber": card.get('collector_number', ''),
        "Set": set_code,
        "Sets": all_sets,
        "Colors": colors,
        "Color Identity": color_identity,
        "CMC": cmc,
        "Types": found_types,
        "Price": price_str,
        "Rarity": card.get('rarity', ''),
        # Scryfall border_color: black / white / borderless / silver / gold.
        # Surfaced so the sort session UI can show it alongside the
        # live card info (collector number, foil status).
        "Border": card.get('border_color', ''),
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
