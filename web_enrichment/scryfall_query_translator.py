# web_enrichment/scryfall_query_translator.py
# ---------------------------------------------------------------------------
# Translate our internal DSL into a Scryfall search query string.
#
# Used by:
#   - Sort-config bin-query "view on Scryfall" button
#   - Query Helper's "view on Scryfall" cross-link
#   - Future "open this saved filter on Scryfall" actions
#
# Phase 4 of plans/query_helper_plan.md (small, dependency-free precursor).
#
# Approach:
#   - Parse the DSL string with the existing query_parser AST.
#   - Walk the AST, mapping each FieldQuery to a Scryfall token (or
#     dropping it with a human-readable reason).
#   - Render AND/OR/NOT combinations using Scryfall's syntax (juxtaposition
#     for AND, lowercase `or`, leading `-` for NOT, parens for grouping).
#
# Predicates that don't translate:
#   Enrichment-only — staple, salt, combo, buylist, cull, deck, wishlist
#   Scan-time      — is:foilscan, is:detected_foil
# ---------------------------------------------------------------------------

from __future__ import annotations

from typing import Tuple, List

# Late import inside translate functions to avoid pulling the parser into
# every caller's import graph. The parser module is small but lives in the
# project root and is technically optional for some downstream callers.


# ---------------------------------------------------------------------------
# DSL field -> Scryfall field code map
# ---------------------------------------------------------------------------
# Most fields pass through with the same canonical Scryfall short code.
# Our parser already aliases many forms ("c" / "color", "t" / "type", etc.)
# down to a single canonical name (see FIELD_ALIASES in query_parser.py),
# so this map is keyed on the canonical names.
_DSL_TO_SCRYFALL_FIELD: dict[str, str] = {
    "color":           "c",
    "color_identity":  "id",
    "type":            "t",
    "mana_value":      "cmc",
    "set":             "s",
    "rarity":          "r",
    "price":           "usd",
    "power":           "pow",
    "toughness":       "tou",
    "oracle":          "o",
    "name":            "name",
    "keyword":         "keyword",
    "otag":            "otag",
    "legal":           "f",
    "produces":        "produces",
    "set_type":        "st",
}

# is:X predicates we explicitly drop because they're scan-time only.
_IS_DROP: frozenset[str] = frozenset({
    "foilscan", "detected_foil",
})

# Whole fields that are enrichment-only and can't map to Scryfall.
_DROP_FIELDS: frozenset[str] = frozenset({
    "staple", "salt", "combo", "buylist", "cull", "deck", "wishlist",
})

# Description templates for dropped clauses — used in the tooltip.
_DROP_REASON: dict[str, str] = {
    "staple":   "enrichment-only (EDHREC staple tier)",
    "salt":     "enrichment-only (EDHREC salt score)",
    "combo":    "enrichment-only (Commander Spellbook combo membership)",
    "buylist":  "enrichment-only (Card Kingdom buylist)",
    "cull":     "enrichment-only (dead-weight flag)",
    "deck":     "enrichment-only (Moxfield deck membership)",
    "wishlist": "enrichment-only (Moxfield wishlist membership)",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def to_scryfall(dsl: str) -> Tuple[str, List[str]]:
    """Translate our DSL into a Scryfall query string.

    Returns ``(scryfall_query, dropped)`` where ``dropped`` is a list of
    short human-readable strings, one per predicate that couldn't be
    translated. Caller should surface these in a tooltip alongside the
    "view on Scryfall" link so the user knows the preview is approximate.

    On parse error, returns ``("", [<error string>])``.
    """
    if not dsl or not dsl.strip():
        return ("", [])

    try:
        from query_parser import parse_query
        ast = parse_query(dsl)
    except Exception as e:  # noqa: BLE001
        return ("", [f"Couldn't parse query: {e}"])

    dropped: List[str] = []
    rendered = _render_node(ast, dropped)
    return (rendered, dropped)


def to_scryfall_url(dsl: str) -> Tuple[str, List[str]]:
    """Convenience: translate ``dsl`` and wrap in a Scryfall search URL.

    Returns ``(url, dropped)``. ``url`` is empty when the translated query
    has nothing left to send.
    """
    from urllib.parse import quote
    q, dropped = to_scryfall(dsl)
    if not q.strip():
        return ("", dropped)
    return (f"https://scryfall.com/search?q={quote(q)}", dropped)


# ---------------------------------------------------------------------------
# AST walk
# ---------------------------------------------------------------------------

def _render_node(node, dropped: List[str]) -> str:
    # Local imports keep query_parser optional for callers that only want
    # the typed-token tables.
    from query_parser import AndNode, OrNode, NotNode, FieldQuery

    if isinstance(node, AndNode):
        rendered = [_render_node(c, dropped) for c in node.children]
        return _join_and(rendered)
    if isinstance(node, OrNode):
        rendered = [_render_node(c, dropped) for c in node.children]
        return _join_or(rendered)
    if isinstance(node, NotNode):
        inner = _render_node(node.child, dropped)
        return _negate(inner)
    if isinstance(node, FieldQuery):
        return _render_field(node, dropped)
    return ""


def _render_field(fq, dropped: List[str]) -> str:
    field = fq.field
    op = fq.operator
    val = fq.value

    if field in _DROP_FIELDS:
        reason = _DROP_REASON.get(field, "not on Scryfall")
        dropped.append(f"{field}:{val}  — {reason}")
        return ""

    if field == "is":
        v = (val or "").lower()
        if v in _IS_DROP:
            dropped.append(f"is:{val}  — scan-time flag (not on Scryfall)")
            return ""
        # Pass through. Scryfall accepts a wide is:* vocabulary; if it
        # doesn't recognize a value, the search returns no results,
        # which is a transparent failure mode.
        return f"is:{v}"

    sf_field = _DSL_TO_SCRYFALL_FIELD.get(field)
    if sf_field is None:
        dropped.append(f"{field}:{val}  — unknown field")
        return ""

    rendered_val = _render_value(val)
    return f"{sf_field}{op}{rendered_val}"


def _render_value(val) -> str:
    """Quote multi-word values; pass single tokens through."""
    if not isinstance(val, str):
        return str(val)
    if " " in val and not (val.startswith('"') and val.endswith('"')):
        # Escape any embedded quotes
        escaped = val.replace('"', '\\"')
        return f'"{escaped}"'
    return val


# ---------------------------------------------------------------------------
# Boolean-tree renderers
# ---------------------------------------------------------------------------
# Scryfall syntax:
#   AND -> juxtaposition (space-separated)
#   OR  -> explicit `or` keyword (lowercase)
#   NOT -> leading `-` on a single term, or `not (...)` on a group
#   Parens for grouping, needed to disambiguate AND/OR mixes
# ---------------------------------------------------------------------------

def _join_and(rendered: List[str]) -> str:
    """Join rendered children with implicit AND (space)."""
    kept = [r for r in rendered if r]
    if not kept:
        return ""
    if len(kept) == 1:
        return kept[0]
    # Wrap any child that is itself a top-level OR so precedence is preserved:
    # `t:creature (c:rg or c:gw)` instead of `t:creature c:rg or c:gw`.
    return " ".join(_paren_if_or(c) for c in kept)


def _join_or(rendered: List[str]) -> str:
    """Join rendered children with explicit ` or `."""
    kept = [r for r in rendered if r]
    if not kept:
        return ""
    if len(kept) == 1:
        return kept[0]
    return " or ".join(kept)


def _negate(inner: str) -> str:
    """Apply Scryfall's NOT: leading `-` for a single term, paren for groups."""
    if not inner:
        return ""
    # Single token (no spaces) -> use leading dash
    if " " not in inner:
        return f"-{inner}"
    # Group -> parenthesize and use -(...)
    return f"-({inner})"


def _paren_if_or(rendered: str) -> str:
    """Wrap a rendered subexpression in parens iff it contains a top-level
    `or`. We approximate top-level by counting parenthesis depth."""
    if " or " not in rendered:
        return rendered
    depth = 0
    for i in range(len(rendered) - 3):
        ch = rendered[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and rendered[i:i + 4] == " or ":
            return f"({rendered})"
    return rendered
