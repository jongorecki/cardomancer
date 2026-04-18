# sorting.py
# Contains logic for sorting cards into bins based on chosen mode.
# Also provides a function to print sorting options.

import json
import cv2

def print_sorting_options():
    """
    Print the menu of sorting options to the console.
    """
    print("Choose your sorting method:")
    print("1 - Color:")
    print("    White (W): Bin 1")
    print("    Blue (U): Bin 2")
    print("    Black (B): Bin 3")
    print("    Red (R): Bin 4")
    print("    Green (G): Bin 5")
    print("    Colorless: Bin 6")
    print("    Multicolor: Bin 7")
    print("    Nonbasic lands: Bin 8")
    print("    Basic lands: Bin 9")
    print("    Errors: Bin 10")
    print()
    print("2 - CMC:")
    print("    1: Bin 1")
    print("    2: Bin 2")
    print("    3: Bin 3")
    print("    4: Bin 4")
    print("    5: Bin 5")
    print("    6: Bin 6")
    print("    7: Bin 7")
    print("    8+: Bin 8")
    print("    Errors: Bin 10")
    print()
    print("3 - Set:")
    print("    KHM: Bin 1")
    print("    NEO: Bin 2")
    print("    Unknown sets: Bin 9")
    print("    Errors: Bin 10")
    print()
    print("4 - Price:")
    print("    Under $0.5: Bin 1")
    print("    $0.5 to $1: Bin 2")
    print("    $1 to $5: Bin 3")
    print("    $5 to $10: Bin 4")
    print("    Above $10: Bin 5")
    print("    Errors: Bin 10")
    print()
    print("5 - Type:")
    print("    Creature: Bin 1")
    print("    Artifact: Bin 2")
    print("    Enchantment: Bin 3")
    print("    Instant: Bin 4")
    print("    Sorcery: Bin 5")
    print("    Battle: Bin 6")
    print("    Planeswalker: Bin 7")
    print("    Land: Bin 8")
    print("    Unknown: Bin 9")
    print("    Errors: Bin 10")
    print()


def draw_info_as_json(frame, info, start_x=10, start_y=30, line_height=20):
    """
    Draw the card info as JSON text on the provided frame for debugging and visualization.
    """
    json_str = json.dumps(info, indent=2)
    lines = json_str.split('\n')
    for i, line in enumerate(lines):
        y = start_y + i * line_height
        cv2.putText(frame, line, (start_x, y),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)


# Functions for determining the bin number based on card attributes
def is_basic_land(name):
    basic_lands = ["plains", "island", "swamp", "mountain", "forest", "wastes"]
    return name.lower() in basic_lands

def is_land_card(types):
    return "land" in types

def get_bin_for_color(info):
    if is_land_card(info.get("Types", [])):
        if is_basic_land(info.get("Name", "")):
            return 9  # Basic lands
        else:
            return 8  # Nonbasic lands

    colors = info.get("Colors", [])
    if not colors:
        return 6  # Colorless
    elif len(colors) > 1:
        return 7  # Multicolor
    else:
        c = colors[0]
        if c == "W": return 1
        if c == "U": return 2
        if c == "B": return 3
        if c == "R": return 4
        if c == "G": return 5
    return 6

def get_bin_for_mana_value(info):
    mv = info.get("CMC", 0)
    if mv <= 1: return 1
    elif mv == 2: return 2
    elif mv == 3: return 3
    elif mv == 4: return 4
    elif mv == 5: return 5
    elif mv == 6: return 6
    elif mv == 7: return 7
    else: return 8

def get_bin_for_set(info):
    set_code = info.get("Set", "???").lower()
    if set_code == "khm": return 1
    elif set_code == "neo": return 2
    else: return 9

def get_bin_for_price(info):
    price_str = info.get("Price", "null")
    if price_str == "null":
        return 10
    try:
        price = float(price_str.strip('$'))
    except:
        return 10

    if price < 0.5: return 1
    elif price < 1.0: return 2
    elif price < 5.0: return 3
    elif price < 10.0: return 4
    else: return 5

def get_bin_for_type(info):
    types = info.get("Types", [])
    if "creature" in types: return 1
    if "artifact" in types: return 2
    if "enchantment" in types: return 3
    if "instant" in types: return 4
    if "sorcery" in types: return 5
    if "battle" in types: return 6
    if "planeswalker" in types: return 7
    if "land" in types: return 8
    return 9

def get_bin_number(info, mode):
    if not info:
        return 10  # Error bin
    if mode == "color":
        return get_bin_for_color(info)
    elif mode == "mana_value":
        return get_bin_for_mana_value(info)
    elif mode == "set":
        return get_bin_for_set(info)
    elif mode == "price":
        return get_bin_for_price(info)
    elif mode == "type":
        return get_bin_for_type(info)
    else:
        return 10  # Default to Error bin if mode unknown
