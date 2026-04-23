# set_symbol_roi.py
# ---------------------------------------------------------------------------
# Phase 5 of the printing-disambiguation pipeline: frame-era -> set-symbol
# ROI lookup.
#
# Set icons sit in a different place on the card depending on the
# frame era Scryfall reports in `card["frame"]` (plus any modifiers in
# `card["frame_effects"]`). This module owns the mapping from
# (frame, frame_effects) -> (x, y, w, h) ROI on the post-rectification
# 745x1040 card image.
#
# Coordinate conventions
# ----------------------
# ROIs are (x, y, w, h) in pixels on a 745x1040 card scan. Origin is
# top-left. w, h are inclusive of anti-aliased fringes at the ROI edge.
# Return value of `None` means "no set symbol to match — skip the
# set-icon stage entirely for this candidate." Phase 6 should fall
# through to the next cascade stage (or cheapest fallback) rather than
# treating None as a miss.
#
# Calibration status
# ------------------
# The pixel values below are *initial estimates* anchored on the hash-DB
# region constants in build_hash_db_v3.py and standard MTG card layout
# (art ends ~y=520, type line runs y=605-655 on modern frames). They
# will need empirical tuning once Phase 7's labeled scan set lands; log
# a `diag_printing.py` pass over a 50-scan sample and adjust ROIs per
# frame to maximize the match signal.
# ---------------------------------------------------------------------------

from typing import Iterable, Optional, Tuple

# Cascade-relevant frame_effects modifiers. When the card's
# frame_effects list intersects these, we may return a different ROI
# (or None) regardless of the base frame.
NO_SYMBOL_EFFECTS = frozenset({
    "borderless",
    # "showcase" varies per treatment; start by treating it as no-symbol
    # and extend on a per-showcase basis as misses show up in validation.
    "showcase",
})

# frame_effects values that do NOT change the icon location — the base
# frame's ROI still applies.
BASE_FRAME_EFFECTS = frozenset({
    "extendedart",
    "inverted",
    "colorshifted",
    "devoid",
    "snow",
    "legendary",
    "miracle",
    "nyxtouched",
    "nyxborn",
    "compasslanddfc",
    "originpwdfc",
    "mooneldrazidfc",
    "waxingandwaningmoondfc",
    "fullart",  # full-art basic lands — no symbol; handled as a
                # frame-specific override where needed
})

# Base ROIs by frame era.
#   1993  — Alpha through 4th Edition: no set symbol exists.
#   1997  — 6th Ed through Scourge: symbol sits in the lower-right of
#           the art box, above the type line.
#   2003  — Mirrodin through M15: symbol on the right of the type line.
#   2015  — M15-onward modern frame: same region as 2003, slightly
#           different vertical offset because the type line band is
#           thinner.
#   future — Future Sight frame (only set that uses it): treat as 2003
#           equivalent for now; the actual symbol position is slightly
#           shifted but the ROI is forgiving.
FRAME_ROI: dict[str, Optional[Tuple[int, int, int, int]]] = {
    "1993": None,
    # 1997 spans classic 1997 layout (symbol at bottom-right of art box,
    # ~y=500-540 for 6ED through Scourge) and modern retro-frame reprints
    # (e.g. RVR, DMR — symbol sits near the type line ~y=600-650).
    "1997": (600, 495, 110, 160),
    # 2003 must be wide enough to cover Mirrodin block's long crescent
    # symbol (x=555-685 on clean PNG).
    "2003": (555, 580, 140, 55),
    # Tight crop — just the symbol rectangle, no bevel/drop-shadow padding.
    # The outer frame border is identical across all 2015 sets, so
    # including it makes matchTemplate score card-frame pixels instead
    # of the symbol itself. Symbol occupies the center ~50x55 of the
    # 70x75 ROI we validated visually.
    "2015": (650, 590, 50, 55),
    "future": (555, 580, 140, 55),
}


def _normalize_effects(
    frame_effects: Optional[Iterable[str]],
) -> frozenset[str]:
    if not frame_effects:
        return frozenset()
    return frozenset(str(e).lower() for e in frame_effects)


def get_symbol_roi(
    frame: Optional[str],
    frame_effects: Optional[Iterable[str]] = None,
) -> Optional[Tuple[int, int, int, int]]:
    """Return (x, y, w, h) ROI for the set symbol on a 745x1040 scan,
    or None when no symbol is expected (or the frame era predates set
    symbols, or the treatment deliberately omits one).

    :param frame: Scryfall `card["frame"]` string — one of "1993",
        "1997", "2003", "2015", "future". Unknown frames return None
        (be conservative; a candidate with no ROI just skips the
        set-icon stage).
    :param frame_effects: Scryfall `card["frame_effects"]` iterable
        (strings). Case-insensitive. When any effect is in
        NO_SYMBOL_EFFECTS (borderless, showcase), return None.
    """
    effects = _normalize_effects(frame_effects)

    if effects & NO_SYMBOL_EFFECTS:
        return None

    if frame is None:
        return None

    return FRAME_ROI.get(str(frame))


def has_symbol(card: dict) -> bool:
    """Shortcut for Phase 6's gating: does this card's printing have a
    set symbol in a known location? If False, set-icon stage should
    skip this candidate.
    """
    return get_symbol_roi(
        card.get("frame"),
        card.get("frame_effects"),
    ) is not None
