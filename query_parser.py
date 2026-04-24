# query_parser.py
# Scryfall-like query parser and evaluator for MTG card sorting.
# Supports field:value syntax, comparison operators, AND/OR/NOT, parentheses,
# quoted strings, and oracle tags (otag:) via pre-fetched cache.
#
# Enrichment tokens (require enrichment_data kwarg to evaluate_query):
#   staple:universal / staple:cedh / staple:archetype / staple:any
#   salt>N  salt<N  salt>=N  salt<=N  salt=N
#   combo:true  combo:any
#   buylist:ck  buylist:ck>=1.00
#   cull:true / cull:false  — dead-weight flag (vanilla/no-staple/no-buylist/not-in-decks)
#   deck:<deck_id>           — card is in the Moxfield deck (any printing)
#   deck:<deck_id>!exact     — card is in the Moxfield deck (exact printing by set+cn)
#   wishlist:<username>      — card is in the user's Moxfield wishlist (any printing)
#   wishlist:<username>!exact — card is in the user's Moxfield wishlist (exact printing)
#
# deck: / wishlist: token notes:
#   - Basic lands are always excluded from deck/wishlist matches (excluded at
#     cache-fill time by MoxfieldSource.import_deck/import_wishlist).
#   - The !exact suffix changes the match key from oracle_id to (set, cn).
#     If the cached entry has no set/cn, it falls back to oracle_id.
#   - These tokens read from the moxfield_decks / moxfield_wishlists tables in
#     enrichment.db via web_enrichment.moxfield.is_in_deck / is_in_wishlist.
#
# otag: hierarchy rollup:
#   Call expand_otag_cache(cache, conn) after building the otag_cache to union
#   descendant-tag oracle_ids into each parent tag's set. conn is an
#   enrichment_db connection; degrades gracefully if tag_catalog is empty.

import re


# ---------------------------------------------------------------------------
# Safe cards-module reference for price lookups
# ---------------------------------------------------------------------------
# We resolve get_art_min_price dynamically (getattr) on each call rather than
# `from cards import get_art_min_price` up front.  Two reasons:
#   1. If cards.py is reloaded via cards.reload_card_data(), a bound name
#      captured at import time would become stale.
#   2. If a running server was started before get_art_min_price was added to
#      cards.py, a `from cards import X` inside _eval_field_query would raise
#      ImportError on every query, sending every card to the fallback bin.
#      Dynamic getattr degrades gracefully: missing function -> None -> we
#      just use the card's own nonfoil price.
try:
    import cards as _cards_module
except ImportError:
    _cards_module = None


def _lookup_art_min_price(card_id):
    """Safely resolve and call cards.get_art_min_price; return None on any issue."""
    if _cards_module is None or not card_id:
        return None
    fn = getattr(_cards_module, 'get_art_min_price', None)
    if fn is None:
        return None
    try:
        return fn(card_id)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# AST Node Types
# ---------------------------------------------------------------------------

class AndNode:
    """All children must match."""
    def __init__(self, children):
        self.children = children

    def __repr__(self):
        return f"AND({', '.join(repr(c) for c in self.children)})"


class OrNode:
    """At least one child must match."""
    def __init__(self, children):
        self.children = children

    def __repr__(self):
        return f"OR({', '.join(repr(c) for c in self.children)})"


class NotNode:
    """Child must NOT match."""
    def __init__(self, child):
        self.child = child

    def __repr__(self):
        return f"NOT({self.child!r})"


class FieldQuery:
    """A single field:operator:value comparison."""
    def __init__(self, field, operator, value):
        self.field = field       # normalized field name (e.g., "color", "mana_value")
        self.operator = operator # ":", "=", "<", ">", "<=", ">="
        self.value = value       # string value (interpretation depends on field)

    def __repr__(self):
        return f"{self.field}{self.operator}{self.value}"


# ---------------------------------------------------------------------------
# Field Aliases — maps user-facing prefixes to canonical field names
# ---------------------------------------------------------------------------

FIELD_ALIASES = {
    "c": "color",
    "color": "color",
    "ci": "color_identity",
    "id": "color_identity",
    "identity": "color_identity",
    "t": "type",
    "type": "type",
    "cmc": "mana_value",
    "mv": "mana_value",
    "manavalue": "mana_value",
    "set": "set",
    "s": "set",
    "e": "set",
    "edition": "set",
    "r": "rarity",
    "rarity": "rarity",
    "usd": "price",
    "price": "price",
    "pow": "power",
    "power": "power",
    "tou": "toughness",
    "toughness": "toughness",
    "o": "oracle",
    "oracle": "oracle",
    "name": "name",
    "is": "is",
    "kw": "keyword",
    "keyword": "keyword",
    "otag": "otag",
    "oracletag": "otag",
    "function": "otag",
    "legal": "legal",
    "format": "legal",
    "f": "legal",
    "produces": "produces",
    "st": "set_type",
    "settype": "set_type",
    # Enrichment tokens (require enrichment_data kwarg in evaluate_query)
    "staple": "staple",
    "salt": "salt",
    "combo": "combo",
    "buylist": "buylist",
    "cull": "cull",
    # Moxfield deck / wishlist membership tokens
    # Syntax:  deck:<deck_id>  or  deck:<deck_id>!exact
    #          wishlist:<username>  or  wishlist:<username>!exact
    "deck": "deck",
    "wishlist": "wishlist",
}


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class QueryParseError(Exception):
    """Raised when query syntax is invalid."""
    pass


class Token:
    def __init__(self, type_, value):
        self.type = type_
        self.value = value

    def __repr__(self):
        return f"Token({self.type}, {self.value!r})"


def tokenize(query_string):
    """
    Convert a query string into a list of tokens.

    Token types:
        LPAREN, RPAREN — grouping
        OR             — the 'or' keyword
        NOT            — the '-' prefix
        FIELD_QUERY    — a field:value or field>=value etc. (stored as tuple)
    """
    tokens = []
    i = 0
    s = query_string.strip()

    while i < len(s):
        ch = s[i]

        # Skip whitespace
        if ch in ' \t':
            i += 1
            continue

        # Parentheses
        if ch == '(':
            tokens.append(Token('LPAREN', '('))
            i += 1
            continue
        if ch == ')':
            tokens.append(Token('RPAREN', ')'))
            i += 1
            continue

        # NOT prefix: '-' followed by a letter (field query like -t:land)
        # This works at start of query, after whitespace, after any token
        if ch == '-' and i + 1 < len(s) and s[i + 1].isalpha():
            tokens.append(Token('NOT', '-'))
            i += 1
            continue

        # Read a word (everything up to whitespace, parens, or end)
        word_start = i
        if ch == '"':
            # Quoted string as a bare value — unlikely but handle gracefully
            i += 1
            while i < len(s) and s[i] != '"':
                i += 1
            if i < len(s):
                i += 1  # skip closing quote
            word = s[word_start:i]
        else:
            while i < len(s) and s[i] not in ' \t()':
                # Handle quoted values within a field query (e.g., o:"draw a card")
                if s[i] == '"':
                    i += 1
                    while i < len(s) and s[i] != '"':
                        i += 1
                    if i < len(s):
                        i += 1  # skip closing quote
                else:
                    i += 1
            word = s[word_start:i]

        if not word:
            continue

        # Check for OR keyword
        if word.lower() == 'or':
            tokens.append(Token('OR', 'or'))
            continue

        # Check for NOT keyword
        if word.lower() == 'not':
            tokens.append(Token('NOT', 'not'))
            continue

        # Try to parse as field:value or field>=value etc.
        field_match = re.match(
            r'^([a-zA-Z]+)'        # field name
            r'([:=]|[<>]=?|!=)'    # operator
            r'(.+)$',              # value
            word
        )
        if field_match:
            field_name = field_match.group(1).lower()
            operator = field_match.group(2)
            value = field_match.group(3)

            # Strip quotes from value if present
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]

            # Normalize operator: treat ':' same as '=' for most fields
            canonical_field = FIELD_ALIASES.get(field_name)
            if canonical_field is None:
                raise QueryParseError(
                    f"Unknown field '{field_name}'. "
                    f"Valid fields: {', '.join(sorted(set(FIELD_ALIASES.values())))}"
                )

            tokens.append(Token('FIELD_QUERY', (canonical_field, operator, value)))
            continue

        # If it doesn't match field:value, treat as a bare name search
        # (Scryfall treats bare words as name searches)
        value = word
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        tokens.append(Token('FIELD_QUERY', ('name', ':', value)))

    return tokens


# ---------------------------------------------------------------------------
# Recursive Descent Parser
# ---------------------------------------------------------------------------

class Parser:
    """
    Grammar:
        expression := or_expr
        or_expr    := and_expr ('or' and_expr)*
        and_expr   := not_expr (not_expr)*
        not_expr   := '-' atom | 'not' atom | atom
        atom       := '(' expression ')' | FIELD_QUERY
    """

    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        if self.pos < len(self.tokens):
            return self.tokens[self.pos]
        return None

    def consume(self, expected_type=None):
        tok = self.peek()
        if tok is None:
            raise QueryParseError("Unexpected end of query")
        if expected_type and tok.type != expected_type:
            raise QueryParseError(
                f"Expected {expected_type}, got {tok.type} ({tok.value!r})"
            )
        self.pos += 1
        return tok

    def parse(self):
        if not self.tokens:
            raise QueryParseError("Empty query")
        result = self.parse_or_expr()
        if self.pos < len(self.tokens):
            raise QueryParseError(
                f"Unexpected token: {self.tokens[self.pos]!r}"
            )
        return result

    def parse_or_expr(self):
        children = [self.parse_and_expr()]
        while self.peek() and self.peek().type == 'OR':
            self.consume('OR')
            children.append(self.parse_and_expr())
        if len(children) == 1:
            return children[0]
        return OrNode(children)

    def parse_and_expr(self):
        children = [self.parse_not_expr()]
        # Implicit AND: keep consuming while next token is NOT, FIELD_QUERY, or LPAREN
        while self.peek() and self.peek().type in ('NOT', 'FIELD_QUERY', 'LPAREN'):
            children.append(self.parse_not_expr())
        if len(children) == 1:
            return children[0]
        return AndNode(children)

    def parse_not_expr(self):
        if self.peek() and self.peek().type == 'NOT':
            self.consume('NOT')
            child = self.parse_atom()
            return NotNode(child)
        return self.parse_atom()

    def parse_atom(self):
        tok = self.peek()
        if tok is None:
            raise QueryParseError("Unexpected end of query")

        if tok.type == 'LPAREN':
            self.consume('LPAREN')
            expr = self.parse_or_expr()
            self.consume('RPAREN')
            return expr

        if tok.type == 'FIELD_QUERY':
            self.consume('FIELD_QUERY')
            field, operator, value = tok.value
            return FieldQuery(field, operator, value)

        raise QueryParseError(f"Unexpected token: {tok!r}")


def parse_query(query_string):
    """
    Parse a Scryfall-like query string into an AST.
    Raises QueryParseError on invalid syntax.
    """
    tokens = tokenize(query_string)
    parser = Parser(tokens)
    return parser.parse()


# ---------------------------------------------------------------------------
# AST Utilities
# ---------------------------------------------------------------------------

def collect_otag_terms(ast):
    """
    Walk the AST and collect all otag: values that need to be pre-fetched
    from the Scryfall API.
    """
    tags = set()
    if isinstance(ast, FieldQuery):
        if ast.field == 'otag':
            tags.add(ast.value.lower())
    elif isinstance(ast, NotNode):
        tags.update(collect_otag_terms(ast.child))
    elif isinstance(ast, (AndNode, OrNode)):
        for child in ast.children:
            tags.update(collect_otag_terms(child))
    return tags


def collect_enrichment_fields(ast) -> set:
    """Walk the AST and return which enrichment fields are used.

    Returns a subset of:
      {'staple', 'salt', 'combo', 'buylist', 'cull', 'deck', 'wishlist'}.
    Used by SortConfig to decide whether to fetch enrichment data during
    bin evaluation.
    """
    ENRICHMENT = frozenset(('staple', 'salt', 'combo', 'buylist', 'cull',
                             'deck', 'wishlist'))
    fields = set()
    if isinstance(ast, FieldQuery):
        if ast.field in ENRICHMENT:
            fields.add(ast.field)
    elif isinstance(ast, NotNode):
        fields.update(collect_enrichment_fields(ast.child))
    elif isinstance(ast, (AndNode, OrNode)):
        for child in ast.children:
            fields.update(collect_enrichment_fields(child))
    return fields


# ---------------------------------------------------------------------------
# Evaluator
# ---------------------------------------------------------------------------

# Rarity ordering for comparison operators
RARITY_ORDER = {
    'common': 0,
    'uncommon': 1,
    'rare': 2,
    'mythic': 3,
    'special': 4,
    'bonus': 5,
}


def _compare(actual, op, target):
    """Generic numeric/ordinal comparison."""
    if actual is None:
        return False
    try:
        actual = float(actual)
        target = float(target)
    except (ValueError, TypeError):
        return False

    if op in (':', '='):
        return actual == target
    elif op == '<':
        return actual < target
    elif op == '>':
        return actual > target
    elif op == '<=':
        return actual <= target
    elif op == '>=':
        return actual >= target
    elif op == '!=':
        return actual != target
    return False


def _eval_color_field(card_data, field_key, operator, value):
    """
    Evaluate color/color_identity queries.
    field_key is 'colors' or 'color_identity' (the Scryfall JSON key).
    """
    colors = card_data.get(field_key, []) or []
    value_lower = value.lower()

    # Special values
    if value_lower == 'm' or value_lower == 'multicolor':
        result = len(colors) >= 2
        return result if operator in (':', '=') else not result

    if value_lower == 'c' or value_lower == 'colorless':
        result = len(colors) == 0
        return result if operator in (':', '=') else not result

    # Numeric comparison on color count
    if operator in ('<', '>', '<=', '>=', '!='):
        return _compare(len(colors), operator, value)

    # Color letter check: c:w means "has white"
    # c=w means "is exactly white" (only white)
    value_upper = value.upper()
    color_letters = set(value_upper)

    if operator == '=':
        # Exact match: card's colors must be exactly these letters
        return set(colors) == color_letters
    else:
        # Contains: card must have all specified colors
        return color_letters.issubset(set(colors))


def _parse_moxfield_token(val: str) -> tuple:
    """Parse a deck:<id> or wishlist:<username> token value.

    The value may optionally end with "!exact" to indicate exact printing mode.

    Returns:
        (base_id, printing_mode) where printing_mode is "any" or "exact".

    Examples:
        "lzbasAFQhEqY5x5SmJRZ9w"        -> ("lzbasAFQhEqY5x5SmJRZ9w", "any")
        "lzbasAFQhEqY5x5SmJRZ9w!exact"  -> ("lzbasAFQhEqY5x5SmJRZ9w", "exact")
        "testuser!exact"                 -> ("testuser", "exact")
    """
    if val.endswith("!exact"):
        return val[:-6], "exact"
    return val, "any"


def _eval_field_query(fq, card_data, otag_cache=None, enrichment_data=None):
    """Evaluate a single FieldQuery against a Scryfall card dict."""

    field = fq.field
    op = fq.operator
    val = fq.value

    # --- Color ---
    if field == 'color':
        return _eval_color_field(card_data, 'colors', op, val)

    # --- Color Identity ---
    if field == 'color_identity':
        return _eval_color_field(card_data, 'color_identity', op, val)

    # --- Type ---
    if field == 'type':
        type_line = (card_data.get('type_line') or '').lower()
        return val.lower() in type_line

    # --- Mana Value ---
    if field == 'mana_value':
        cmc = card_data.get('cmc')
        return _compare(cmc, op, val)

    # --- Set ---
    if field == 'set':
        card_set = (card_data.get('set') or '').lower()
        val_lower = val.lower()
        if op in (':', '='):
            return card_set == val_lower
        elif op == '!=':
            return card_set != val_lower
        return False

    # --- Rarity ---
    if field == 'rarity':
        card_rarity = (card_data.get('rarity') or '').lower()
        val_lower = val.lower()
        if op in (':', '='):
            return card_rarity == val_lower
        elif op == '!=':
            return card_rarity != val_lower
        # Comparison operators use ordinal ranking
        card_ord = RARITY_ORDER.get(card_rarity)
        val_ord = RARITY_ORDER.get(val_lower)
        if card_ord is not None and val_ord is not None:
            return _compare(card_ord, op, val_ord)
        return False

    # --- Price ---
    if field == 'price':
        # Use art-min-price (cheapest nonfoil across same-art printings) when
        # available, so a $10 promo printing sorts like its 50-cent siblings.
        # Resolved via _lookup_art_min_price (safe getattr) to survive module
        # reloads and stale-module ImportError.
        card_id = card_data.get('id')
        art_price = _lookup_art_min_price(card_id)
        if art_price is not None:
            return _compare(art_price, op, val)
        # Fallback: use the card's own nonfoil price
        prices = card_data.get('prices') or {}
        usd = prices.get('usd')
        if usd is None:
            return False
        return _compare(usd, op, val)

    # --- Power ---
    if field == 'power':
        power = card_data.get('power')
        if power is None or power == '*':
            return False
        return _compare(power, op, val)

    # --- Toughness ---
    if field == 'toughness':
        toughness = card_data.get('toughness')
        if toughness is None or toughness == '*':
            return False
        return _compare(toughness, op, val)

    # --- Oracle Text ---
    if field == 'oracle':
        oracle_text = (card_data.get('oracle_text') or '').lower()
        return val.lower() in oracle_text

    # --- Name ---
    if field == 'name':
        name = (card_data.get('name') or '').lower()
        return val.lower() in name

    # --- Keywords ---
    if field == 'keyword':
        keywords = card_data.get('keywords') or []
        val_lower = val.lower()
        return any(val_lower == kw.lower() for kw in keywords)

    # --- Oracle Tags (pre-fetched) ---
    if field == 'otag':
        if otag_cache is None:
            return False
        tag_set = otag_cache.get(val.lower())
        if tag_set is None:
            return False
        oracle_id = card_data.get('oracle_id')
        return oracle_id in tag_set if oracle_id else False

    # --- Format Legality ---
    if field == 'legal':
        legalities = card_data.get('legalities') or {}
        val_lower = val.lower()
        status = legalities.get(val_lower, '')
        if op in (':', '='):
            return status == 'legal' or status == 'restricted'
        elif op == '!=':
            return status not in ('legal', 'restricted')
        return False

    # --- Produces Mana ---
    if field == 'produces':
        produced = card_data.get('produced_mana') or []
        val_upper = val.upper()
        return val_upper in produced

    # --- Set Type ---
    if field == 'set_type':
        set_type = (card_data.get('set_type') or '').lower()
        val_lower = val.lower()
        if op in (':', '='):
            return set_type == val_lower
        elif op == '!=':
            return set_type != val_lower
        return False

    # --- is: special predicates ---
    if field == 'is':
        return _eval_is_predicate(card_data, val)

    # --- Enrichment: staple tier ---
    if field == 'staple':
        if enrichment_data is None:
            return False
        val_lower = val.lower()
        if val_lower == 'any':
            return any(enrichment_data.get(f"staple_{t}", False)
                       for t in ("universal", "cedh", "archetype"))
        return bool(enrichment_data.get(f"staple_{val_lower}", False))

    # --- Enrichment: salt score ---
    if field == 'salt':
        if enrichment_data is None:
            return False
        return _compare(enrichment_data.get("salt"), op, val)

    # --- Enrichment: combo membership ---
    if field == 'combo':
        if enrichment_data is None:
            return False
        val_lower = val.lower()
        if val_lower in ('true', 'any', 'yes'):
            return bool(enrichment_data.get("in_combo", False))
        if val_lower in ('false', 'no'):
            return not bool(enrichment_data.get("in_combo", False))
        return False

    # --- Enrichment: CardKingdom buylist price ---
    # Syntax:
    #   buylist:ck           — any card with a CK buylist entry (price > 0)
    #   buylist:ck>=1.00     — CK buylist price >= $1.00
    #   buylist:ck>0.50      — CK buylist price > $0.50
    #   -buylist:ck          — no CK buylist entry
    if field == 'buylist':
        if enrichment_data is None:
            return False
        val_lower = val.lower()

        # Parse vendor prefix (currently only 'ck' supported)
        # Accept bare "ck" (presence check) or "ck>=N" / "ck>N" etc.
        # The tokenizer hands us the full val string after "buylist:"
        # e.g.  buylist:ck        -> val = "ck"
        #       buylist:ck>=1.00  -> val = "ck>=1.00"  (captured by tokenizer
        #                            as a single word since no whitespace)
        ck_price = enrichment_data.get("buylist_ck_price")  # float | None

        # Strip the vendor prefix to get the optional comparison suffix
        if val_lower.startswith("ck"):
            suffix = val[2:].strip()   # e.g. "" / ">=1.00" / ">0.50"
        else:
            # Unknown vendor — no match
            return False

        if not suffix:
            # Bare "buylist:ck" — presence check: any non-None, non-zero price
            return ck_price is not None and ck_price > 0

        # Comparison: extract operator + number
        import re as _re
        m = _re.match(r'^(>=|<=|!=|>|<|=)(.+)$', suffix)
        if not m:
            return False
        cmp_op, cmp_val = m.group(1), m.group(2)
        if ck_price is None:
            return False
        return _compare(ck_price, cmp_op, cmp_val)

    # --- Enrichment: dead-weight cull flag ---
    # Syntax:
    #   cull:true  — card is a dead-weight cull candidate (all 3 predicates true)
    #   cull:false — card is NOT a cull candidate (negation of the above)
    #
    # A card is a cull candidate when ALL of:
    #   1. Vanilla or french-vanilla (oracle_text empty OR only keyword abilities).
    #      Evaluated directly from card_data['oracle_text'] — no enrichment needed.
    #   2. NOT a staple at any tier (staple:any is false).
    #   3. NOT on the CardKingdom buylist (buylist_ck_price is None or 0).
    #   4. NOT in any of the user's Moxfield decks.
    #      TODO: AND with deck-usage lookup once Phase 3 item 3.18 (Moxfield
    #      deck-usage overlay) is implemented.  For now treated as trivially
    #      true (we conservatively assume the card is not in any deck unless
    #      proven otherwise — this means the cull flag is *looser* until 3.18
    #      lands, but never produces false negatives once deck data exists).
    if field == 'cull':
        val_lower = val.lower()
        want_cull = val_lower in ('true', 'yes', '1')
        want_not_cull = val_lower in ('false', 'no', '0')
        if not want_cull and not want_not_cull:
            return False

        # Predicate 1: vanilla / french-vanilla
        from web_enrichment.vanilla import is_vanilla_or_french_vanilla
        oracle_text = card_data.get('oracle_text') or ''
        is_vanilla = is_vanilla_or_french_vanilla(oracle_text)

        if not is_vanilla:
            # Card has complex text → definitely not a cull candidate
            return not want_cull  # True if cull:false, False if cull:true

        # Predicate 2: NOT a staple at any tier
        if enrichment_data is not None:
            is_staple = any(enrichment_data.get(f"staple_{t}", False)
                            for t in ("universal", "cedh", "archetype"))
        else:
            # No enrichment data available — conservatively assume not a staple
            is_staple = False

        if is_staple:
            return not want_cull

        # Predicate 3: NOT on CK buylist
        if enrichment_data is not None:
            ck_price = enrichment_data.get("buylist_ck_price")
            on_buylist = (ck_price is not None and ck_price > 0)
        else:
            on_buylist = False

        if on_buylist:
            return not want_cull

        # Predicate 4: NOT in any of the user's Moxfield decks.
        # TODO: AND with deck-usage lookup once Phase 3 item 3.18 lands.
        # in_deck = enrichment_data.get("in_deck", False) if enrichment_data else False
        # if in_deck:
        #     return not want_cull
        in_deck = False  # trivially False until 3.18 is implemented

        # All predicates satisfied — this IS a cull candidate
        is_cull = is_vanilla and not is_staple and not on_buylist and not in_deck
        return is_cull if want_cull else not is_cull

    # --- Moxfield deck membership ---
    # Syntax:
    #   deck:<deck_id>         — card is in the deck (any printing)
    #   deck:<deck_id>!exact   — card is in the deck (exact printing by set+cn)
    #
    # The deck must have been previously imported and cached via
    # /api/integrations/moxfield/deck/import.  If not cached, returns False.
    # Basic lands are excluded from the deck cache by import_deck().
    if field == 'deck':
        oracle_id = card_data.get('oracle_id') or ''
        if not oracle_id:
            return False
        # Parse the !exact suffix from the value.
        deck_id, printing_mode = _parse_moxfield_token(val)
        set_code = (card_data.get('set') or '').lower()
        cn = card_data.get('collector_number') or ''
        try:
            from web_enrichment.moxfield import is_in_deck, PRINTING_MODE_ANY, PRINTING_MODE_EXACT
            return is_in_deck(
                deck_id,
                oracle_id,
                printing_mode=printing_mode,
                set_code=set_code,
                collector_number=cn,
            )
        except Exception:
            return False

    # --- Moxfield wishlist membership ---
    # Syntax:
    #   wishlist:<username>         — card is in the user's wishlist (any printing)
    #   wishlist:<username>!exact   — card is in the wishlist (exact printing)
    #
    # The wishlist must have been previously imported and cached via
    # /api/integrations/moxfield/wishlist/import.  If not cached, returns False.
    # Basic lands are excluded from the wishlist cache by import_wishlist().
    if field == 'wishlist':
        oracle_id = card_data.get('oracle_id') or ''
        if not oracle_id:
            return False
        username, printing_mode = _parse_moxfield_token(val)
        set_code = (card_data.get('set') or '').lower()
        cn = card_data.get('collector_number') or ''
        try:
            from web_enrichment.moxfield import is_in_wishlist, PRINTING_MODE_ANY, PRINTING_MODE_EXACT
            return is_in_wishlist(
                username,
                oracle_id,
                printing_mode=printing_mode,
                set_code=set_code,
                collector_number=cn,
            )
        except Exception:
            return False

    return False


def _eval_is_predicate(card_data, predicate):
    """Evaluate an is:X predicate."""
    pred = predicate.lower()
    type_line = (card_data.get('type_line') or '').lower()
    colors = card_data.get('colors') or []
    supertypes = type_line.split(' — ')[0] if ' — ' in type_line else type_line

    if pred == 'land':
        return 'land' in type_line
    elif pred == 'creature':
        return 'creature' in type_line
    elif pred == 'artifact':
        return 'artifact' in type_line
    elif pred == 'enchantment':
        return 'enchantment' in type_line
    elif pred == 'instant':
        return 'instant' in type_line
    elif pred == 'sorcery':
        return 'sorcery' in type_line
    elif pred == 'planeswalker':
        return 'planeswalker' in type_line
    elif pred == 'battle':
        return 'battle' in type_line
    elif pred == 'spell':
        return 'land' not in type_line
    elif pred == 'permanent':
        permanent_types = ['creature', 'artifact', 'enchantment',
                          'planeswalker', 'battle', 'land']
        return any(t in type_line for t in permanent_types)
    elif pred == 'historic':
        return ('legendary' in supertypes or 'artifact' in type_line
                or 'saga' in type_line)
    elif pred == 'basic':
        return 'basic' in supertypes
    elif pred == 'legendary':
        return 'legendary' in supertypes
    elif pred == 'multicolor' or pred == 'multi' or pred == 'gold':
        return len(colors) >= 2
    elif pred == 'colorless':
        return len(colors) == 0
    elif pred == 'mono' or pred == 'monocolor':
        return len(colors) == 1
    elif pred == 'token':
        layout = (card_data.get('layout') or '').lower()
        return layout == 'token'
    elif pred == 'promo':
        return card_data.get('promo', False)
    elif pred == 'reprint':
        return card_data.get('reprint', False)
    elif pred == 'fullart' or pred == 'full_art':
        return card_data.get('full_art', False)
    elif pred == 'foil':
        # Scryfall capability flag: "a foil printing of this card exists".
        # Matches is:foil semantics on Scryfall itself. For "this scanned
        # card is physically a foil printing", use is:foilscan.
        return card_data.get('foil', False)
    elif pred == 'nonfoil':
        return card_data.get('nonfoil', False)
    elif pred == 'foilscan' or pred == 'detected_foil':
        # Matches when the scanned card has been detected as foil by
        # foil_detect (populated into card_data['is_foil'] by the web
        # worker / scan tracker). Distinct from is:foil (capability flag).
        return bool(card_data.get('is_foil', False))
    elif pred == 'digital':
        return card_data.get('digital', False)
    elif pred == 'reserved':
        return card_data.get('reserved', False)

    return False


def evaluate_query(ast, card_data, otag_cache=None, enrichment_data=None):
    """
    Evaluate a parsed query AST against a full Scryfall card dict.
    Returns True if the card matches the query.

    otag_cache:      optional dict of { tag_name: set(oracle_ids) } for otag: queries.
    enrichment_data: optional per-card dict for enrichment tokens:
                       staple_universal, staple_cedh, staple_archetype: bool
                       salt: float | None
                       in_combo: bool
                       buylist_ck_price: float | None
    """
    if isinstance(ast, FieldQuery):
        return _eval_field_query(ast, card_data, otag_cache, enrichment_data)

    elif isinstance(ast, AndNode):
        return all(evaluate_query(c, card_data, otag_cache, enrichment_data)
                   for c in ast.children)

    elif isinstance(ast, OrNode):
        return any(evaluate_query(c, card_data, otag_cache, enrichment_data)
                   for c in ast.children)

    elif isinstance(ast, NotNode):
        return not evaluate_query(ast.child, card_data, otag_cache, enrichment_data)

    raise QueryParseError(f"Unknown AST node type: {type(ast)}")


# ---------------------------------------------------------------------------
# Convenience: parse + evaluate in one call
# ---------------------------------------------------------------------------

def matches_query(query_string, card_data, otag_cache=None, enrichment_data=None):
    """Parse a query string and evaluate it against a card. Returns bool."""
    ast = parse_query(query_string)
    return evaluate_query(ast, card_data, otag_cache, enrichment_data)


# ---------------------------------------------------------------------------
# otag hierarchy rollup
# ---------------------------------------------------------------------------

def expand_otag_cache(cache: dict, conn) -> dict:
    """Union descendant-tag oracle_ids into each parent tag's set.

    Reads tag_catalog from an enrichment_db connection to build the
    parent→children map, then loads oracle_ids for each descendant from
    the tags table.  Modifies and returns an expanded copy of cache.
    Degrades gracefully (returns cache unchanged) if the DB tables are empty
    or unavailable.
    """
    try:
        catalog_rows = conn.execute(
            "SELECT tag_name, parent FROM tag_catalog WHERE parent IS NOT NULL"
        ).fetchall()
    except Exception:
        return cache

    if not catalog_rows:
        return cache

    # Build parent → [children] map
    children_map: dict[str, list[str]] = {}
    for row in catalog_rows:
        tag_name = row[0] if not hasattr(row, "keys") else row["tag_name"]
        parent   = row[1] if not hasattr(row, "keys") else row["parent"]
        children_map.setdefault(parent, []).append(tag_name)

    def _descendants(tag: str) -> list[str]:
        result: list[str] = []
        for child in children_map.get(tag, []):
            result.append(child)
            result.extend(_descendants(child))
        return result

    expanded = {k: set(v) for k, v in cache.items()}
    for tag in list(cache.keys()):
        descs = _descendants(tag)
        if not descs:
            continue
        placeholders = ",".join("?" * len(descs))
        try:
            rows = conn.execute(
                f"SELECT oracle_id FROM tags WHERE tag_name IN ({placeholders})",
                descs,
            ).fetchall()
            for row in rows:
                expanded[tag].add(row[0])
        except Exception:
            pass

    return expanded
