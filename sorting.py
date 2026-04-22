# sorting.py
# ---------------------------------------------------------------------------
# Sorting state + shared display helpers.
#
# Prior to 2026-04-22 this module also contained per-mode bin-assignment
# helpers (get_bin_for_color, get_bin_for_price, ...) dispatched by
# get_bin_number(info, mode). Sort routing is now unified: every session
# runs through a SortConfig loaded from sort_configs/*.txt (or inline
# from the UI). The built-in modes (color / mana_value / set / price /
# type) are shipped as regular .txt files in sort_configs/.
# ---------------------------------------------------------------------------

import json

import cv2

# Active custom sort config — set by web_worker / main when a session starts.
_active_sort_config = None


def set_sort_config(config):
    """Set the active sort configuration (called by session start)."""
    global _active_sort_config
    _active_sort_config = config


def get_sort_config():
    """Get the active sort configuration, or None if no session is running."""
    return _active_sort_config


def print_sorting_options():
    """Print the menu of sorting options to the console (CLI entry point).

    Built-in modes (color / mana_value / set / price / type) are backed by
    sort_configs/*.txt files — the output below is just a one-line teaser
    for each. Full bin definitions live in the .txt files.
    """
    print("Choose your sorting method:")
    print("1 - Color        (sort_configs/color.txt)")
    print("2 - Mana Value   (sort_configs/mana_value.txt)")
    print("3 - Set          (sort_configs/set.txt)")
    print("4 - Price Tiers  (sort_configs/price.txt)")
    print("5 - Card Type    (sort_configs/type.txt)")
    print()
    print("6 - Custom (from file):  Load a custom sort_configs/*.txt file.")
    print("7 - Custom (manual):     Define queries interactively at the prompt.")
    print()
    print("All modes use Scryfall-like query syntax (c:w, t:creature, usd>=10,")
    print("cmc<=3, r:mythic, otag:removal). The `overrides:` directive")
    print("promotes specific bins to be checked first.")
    print()


def draw_info_as_json(frame, info, start_x=10, start_y=30, line_height=20):
    """Draw card info JSON on frame for debugging / visualization."""
    json_str = json.dumps(info, indent=2)
    lines = json_str.split('\n')
    for i, line in enumerate(lines):
        y = start_y + i * line_height
        cv2.putText(frame, line, (start_x, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
