# cards.py
# Handles loading of card data, filtering, and extracting card information.
# Also applies rules for excluded sets, language, and paper availability.

import json
import os
from config import CARDS_JSON_PATH, EXCLUDED_SETS

# Load card data from JSON if available
if os.path.exists(CARDS_JSON_PATH):
    with open(CARDS_JSON_PATH, 'r', encoding='utf-8') as f:
        CARDS_DATA = json.load(f)
    CARD_DATA_BY_ID = {c.get('id'): c for c in CARDS_DATA}
else:
    CARDS_DATA = []
    CARD_DATA_BY_ID = {}


def extract_card_info(card_id):
    """
    Given a card_id, return a dictionary of card info including name, set,
    colors, CMC, type, and price.
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
    usd_price = card.get('prices', {}).get('usd')
    price_str = "null"

    if usd_price:
        try:
            price_float = float(usd_price)
            price_str = f"${price_float:.2f}"
        except:
            pass

    type_line = card.get('type_line', '').lower()
    possible_types = ["creature", "artifact", "enchantment", "instant", "sorcery", "battle", "planeswalker", "land"]
    found_types = [t for t in possible_types if t in type_line]

    info = {
        "Name": name,
        "Set": set_code,
        "Colors": colors,
        "Color Identity": color_identity,
        "CMC": cmc,
        "Types": found_types,
        "Price": price_str
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
