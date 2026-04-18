# ocr.py
# OCR functions for reading text from MTG card images using Tesseract.

import re
import cv2
import numpy as np
import pytesseract

from config import TESSERACT_CMD, TITLE_REGION, COLLECTOR_REGION, CARD_WIDTH, CARD_HEIGHT

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


# ---------------------------------------------------------------------------
# Preprocessing: multiple strategies, pick the one that yields the most text
# ---------------------------------------------------------------------------

def _to_gray(img):
    if len(img.shape) == 3:
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return img.copy()


def _upscale(gray, factor):
    if factor <= 1:
        return gray
    h, w = gray.shape
    return cv2.resize(gray, (w * factor, h * factor), interpolation=cv2.INTER_CUBIC)


def _preprocess_adaptive(gray, block=31, c_val=10):
    """Adaptive Gaussian threshold."""
    return cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, block, c_val
    )


def _preprocess_otsu(gray):
    """Otsu's global threshold — good for high-contrast text."""
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def _preprocess_sharpen_otsu(gray):
    """Sharpen then Otsu — helps with slightly blurry webcam images."""
    kernel = np.array([[-1, -1, -1],
                       [-1,  9, -1],
                       [-1, -1, -1]])
    sharpened = cv2.filter2D(gray, -1, kernel)
    _, binary = cv2.threshold(sharpened, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary


def _preprocess_morpho(gray):
    """Otsu + morphological opening to clean up noise."""
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    _, binary = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
    return cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel)


PREPROCESS_STRATEGIES = [
    ("adaptive", _preprocess_adaptive),
    ("otsu", _preprocess_otsu),
    ("sharpen_otsu", _preprocess_sharpen_otsu),
    ("morpho", _preprocess_morpho),
]


def _ocr_with_best_strategy(region_img, scale_factor=4, invert=False,
                             psm=7, debug=False, debug_label=""):
    """
    Try multiple preprocessing strategies on a region, OCR each one,
    and return the result with the most alphanumeric characters.
    """
    gray = _to_gray(region_img)
    gray = _upscale(gray, scale_factor)
    if invert:
        gray = cv2.bitwise_not(gray)

    best_text = ""
    best_score = -1
    best_name = ""

    for name, preprocess_fn in PREPROCESS_STRATEGIES:
        binary = preprocess_fn(gray)
        text = pytesseract.image_to_string(binary, config=f'--psm {psm}').strip()
        score = sum(c.isalnum() for c in text)

        if debug:
            inv_label = "_inv" if invert else ""
            cv2.imshow(f"DEBUG: {debug_label} {name}{inv_label}", binary)
            print(f"  [{debug_label} {name}{inv_label}] '{text}' (score={score})")

        if score > best_score:
            best_score = score
            best_text = text
            best_name = name

    if debug and best_text:
        inv_label = "_inv" if invert else ""
        print(f"  [{debug_label} BEST{inv_label}] '{best_text}' via {best_name}")

    return best_text


# ---------------------------------------------------------------------------
# Region cropping
# ---------------------------------------------------------------------------

def crop_region(card_img, region):
    """
    Crop a region from a canonical card image.
    region: (x_frac, y_frac, w_frac, h_frac) as fractions of card dimensions.
    """
    h, w = card_img.shape[:2]
    x_frac, y_frac, w_frac, h_frac = region
    x1 = int(x_frac * w)
    y1 = int(y_frac * h)
    x2 = int((x_frac + w_frac) * w)
    y2 = int((y_frac + h_frac) * h)
    return card_img[y1:y2, x1:x2]


# ---------------------------------------------------------------------------
# Title and collector OCR
# ---------------------------------------------------------------------------

def ocr_title(card_img, debug=False):
    """
    OCR the title bar region of a canonical card image.
    Tries multiple preprocessing strategies and returns the best result.
    """
    region_img = crop_region(card_img, TITLE_REGION)

    if debug:
        cv2.imshow("DEBUG: title region (raw)", region_img)

    # Try normal (dark text on light background)
    text_normal = _ocr_with_best_strategy(
        region_img, scale_factor=4, invert=False,
        psm=7, debug=debug, debug_label="title"
    )

    # Also try inverted in case of light text on dark (some card frames)
    text_inv = _ocr_with_best_strategy(
        region_img, scale_factor=4, invert=True,
        psm=7, debug=debug, debug_label="title"
    )

    # Return whichever got more alpha chars
    if sum(c.isalpha() for c in text_inv) > sum(c.isalpha() for c in text_normal):
        return text_inv
    return text_normal


def ocr_collector(card_img, debug=False):
    """
    OCR the collector info region at the bottom-left of a canonical card image.
    Tries multiple preprocessing strategies in both normal and inverted.
    """
    region_img = crop_region(card_img, COLLECTOR_REGION)

    if debug:
        cv2.imshow("DEBUG: collector region (raw)", region_img)

    text_normal = _ocr_with_best_strategy(
        region_img, scale_factor=5, invert=False,
        psm=7, debug=debug, debug_label="coll"
    )
    text_inv = _ocr_with_best_strategy(
        region_img, scale_factor=5, invert=True,
        psm=7, debug=debug, debug_label="coll"
    )

    if debug:
        print(f"[OCR debug] collector normal='{text_normal}' | inverted='{text_inv}'")

    # Return whichever has more alnum content
    if sum(c.isalnum() for c in text_inv) > sum(c.isalnum() for c in text_normal):
        return text_inv
    return text_normal


# ---------------------------------------------------------------------------
# Collector info parsing
# ---------------------------------------------------------------------------

def parse_collector_info(text):
    """
    Parse OCR'd collector info text to extract set code and collector number.

    Modern MTG cards show something like:
      "042/281 · DMU · EN"  or  "042 DMU EN"  or  "DMU · 042/281"

    Returns (set_code, collector_number) or (None, None) if not parseable.
    """
    if not text:
        return None, None

    # Clean up common OCR artifacts
    cleaned = text.replace('·', ' ').replace('|', ' ').replace('•', ' ')
    cleaned = cleaned.strip()

    # Look for collector number pattern: 1-4 digits, optionally /total
    num_match = re.search(r'\b(\d{1,4})(?:/\d{1,4})?\b', cleaned)
    collector_number = num_match.group(1) if num_match else None

    # Look for set code: 3-4 uppercase alphanumeric characters
    # Exclude common false positives like "EN", "FR", "JP" (language codes)
    lang_codes = {'EN', 'FR', 'DE', 'IT', 'ES', 'PT', 'JP', 'JA', 'KO', 'RU', 'ZH'}
    set_match = re.findall(r'\b([A-Z0-9]{3,5})\b', cleaned.upper())
    set_code = None
    for candidate in set_match:
        if candidate not in lang_codes:
            set_code = candidate.lower()
            break

    return set_code, collector_number


# ---------------------------------------------------------------------------
# Orientation detection (hash-based, same approach as the original code)
# ---------------------------------------------------------------------------

def determine_orientation(card_img, crop_size=745, hash_size=16):
    """
    Determine if the card image is upright or upside-down by comparing
    the full card image in both orientations against the hash database.
    Whichever orientation has a closer match is correct.

    Works with both v1 (full crop) and v2 (art region) hash databases.

    Returns (oriented_img, was_rotated).
    """
    from hashing import compute_combined_distances

    rotated = cv2.rotate(card_img, cv2.ROTATE_180)

    # Compare both orientations — compute_combined_distances handles
    # the correct crop (art region for v2, full crop for v1) internally
    upright_dists = compute_combined_distances(card_img, hash_size)
    rotated_dists = compute_combined_distances(rotated, hash_size)

    u_id, u_dist = upright_dists[0] if upright_dists else (None, 999)
    r_id, r_dist = rotated_dists[0] if rotated_dists else (None, 999)

    print(f"[orientation] upright: dist={u_dist:.2f}")
    print(f"[orientation] rotated: dist={r_dist:.2f}")

    if r_dist < u_dist:
        return rotated, True
    return card_img, False
