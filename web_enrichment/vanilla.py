# web_enrichment/vanilla.py
# ---------------------------------------------------------------------------
# Vanilla / French-vanilla detection helper.
#
# A card is "vanilla" if it has no oracle text (empty or null).
# A card is "french-vanilla" if its oracle text consists solely of a
# list of evergreen keyword abilities (e.g. "Flying, Vigilance") with
# no complex clauses.
#
# Rule used here: oracle_text is empty, OR every sentence is a single
# capitalised keyword from the known evergreen / common keyword list.
# A "sentence" is a line or comma-separated item after stripping punctuation.
#
# This helper is intentionally conservative: if the text doesn't cleanly
# decompose into known keywords, it returns False.
# ---------------------------------------------------------------------------

from __future__ import annotations

import re

# Evergreen + common keyword abilities recognised as french-vanilla.
# Case-insensitive match.
_KNOWN_KEYWORDS = frozenset({
    "flying",
    "vigilance",
    "trample",
    "haste",
    "first strike",
    "double strike",
    "deathtouch",
    "lifelink",
    "menace",
    "reach",
    "hexproof",
    "indestructible",
    "defender",
    "flash",
    # Protection forms are NOT included — "protection from X" is complex.
    # If a card has "protection from black" it fails the french-vanilla test.
})


def is_vanilla_or_french_vanilla(oracle_text: str) -> bool:
    """Return True if the card is vanilla (no text) or french-vanilla
    (only keyword abilities, nothing else).

    Parameters
    ----------
    oracle_text:
        The ``oracle_text`` field from Scryfall data.  Pass ``""`` or
        ``None`` (converted to ``""`` by callers) for textless cards.

    Examples
    --------
    >>> is_vanilla_or_french_vanilla("")          # Grizzly Bears
    True
    >>> is_vanilla_or_french_vanilla("Flying, vigilance")    # Serra Angel
    True
    >>> is_vanilla_or_french_vanilla("Flying\\nVigilance")    # also True
    True
    >>> is_vanilla_or_french_vanilla(
    ...     "Lightning Bolt deals 3 damage to any target.")
    False
    >>> is_vanilla_or_french_vanilla("{T}: Add {C}{C}.")     # Sol Ring
    False
    """
    if not oracle_text:
        return True

    text = oracle_text.strip()
    if not text:
        return True

    # Reject immediately if there are any special mana symbols, curly-braces,
    # or punctuation that indicates a triggered/activated ability.
    # Presence of '{', '}', ':', '.', '(' all indicate non-keyword text.
    if re.search(r'[{:(.]', text):
        return False

    # Split on newlines and commas; strip whitespace from each piece.
    # e.g. "Flying, Vigilance\nDeathtouch" → ["Flying", "Vigilance", "Deathtouch"]
    raw_parts = re.split(r'[\n,]+', text)
    parts = [p.strip() for p in raw_parts if p.strip()]

    if not parts:
        return True

    for part in parts:
        if part.lower() not in _KNOWN_KEYWORDS:
            return False

    return True
