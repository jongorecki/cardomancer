import os
import json
import cv2
import numpy as np
from collections import defaultdict
from statistics import mean, pstdev
from concurrent.futures import ProcessPoolExecutor, as_completed

# Configuration
cards_json_path = r"C:\Users\Jon\Downloads\default-cards-20241206100658.json"
images_dir = "downloaded_cards"  # directory where your PNG files are stored
output_signature_json = "layout_signatures.json"

def load_card_data():
    if os.path.exists(cards_json_path):
        with open(cards_json_path, 'r', encoding='utf-8') as f:
            cards_data = json.load(f)
        card_data_by_id = {c.get('id'): c for c in cards_data}
    else:
        print(f"Unable to find {cards_json_path}")
        card_data_by_id = {}
        cards_data = []
    return cards_data, card_data_by_id

def group_cards_by_layout(cards_data):
    cards_by_layout = defaultdict(list)
    for c in cards_data:
        layout = c.get('layout')
        card_id = c.get('id')
        if layout and card_id:
            # Check if image exists
            img_name = card_id + ".png"
            img_path = os.path.join(images_dir, img_name)
            if os.path.exists(img_path):
                cards_by_layout[layout].append(card_id)
    return cards_by_layout

def extract_layout_features(img_path):
    """
    Extract simple contour-based features that might help identify layout.
    Returns a dict of features or None if image can't be loaded.
    """
    img = cv2.imread(img_path)
    if img is None:
        return None

    # Convert to grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # Edge detection
    edges = cv2.Canny(gray, 50, 150)
    # Morph close to combine edges
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3,3))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    areas = []
    aspect_ratios = []
    for cnt in contours:
        area = cv2.contourArea(cnt)
        if area > 100:  # Filter out tiny contours
            areas.append(area)
            x,y,w,h = cv2.boundingRect(cnt)
            if h > 0:
                aspect_ratios.append(w/h)

    if areas:
        mean_area = mean(areas)
        std_area = pstdev(areas) if len(areas) > 1 else 0.0
    else:
        mean_area = 0.0
        std_area = 0.0

    if aspect_ratios:
        mean_ar = mean(aspect_ratios)
        std_ar = pstdev(aspect_ratios) if len(aspect_ratios) > 1 else 0.0
    else:
        mean_ar = 0.0
        std_ar = 0.0

    features = {
        "num_contours": len(areas),
        "mean_area": mean_area,
        "std_area": std_area,
        "mean_aspect_ratio": mean_ar,
        "std_aspect_ratio": std_ar
    }

    return features

def safe_mean(lst):
    return mean(lst) if lst else 0.0

def main():
    cards_data, card_data_by_id = load_card_data()
    cards_by_layout = group_cards_by_layout(cards_data)

    layout_signatures = {}

    for layout, card_ids in cards_by_layout.items():
        print(f"Analyzing layout: {layout} with {len(card_ids)} cards...")
        all_features = []

        # Prepare tasks for parallel execution
        tasks = []
        for cid in card_ids:
            img_name = cid + ".png"
            img_path = os.path.join(images_dir, img_name)
            tasks.append((cid, img_path))

        # Use multiprocessing to speed up feature extraction
        with ProcessPoolExecutor() as executor:
            future_to_cid = {executor.submit(extract_layout_features, t[1]): t[0] for t in tasks}
            for future in as_completed(future_to_cid):
                cid = future_to_cid[future]
                try:
                    features = future.result()
                    if features is not None:
                        all_features.append(features)
                        print(f"  Processed {cid} - success")
                    else:
                        print(f"  Processed {cid} - failure (unable to load image)")
                except Exception as e:
                    print(f"  Processed {cid} - error: {e}")

        if not all_features:
            print(f"No valid features extracted for layout {layout}, skipping.")
            continue

        num_contours_vals = [f['num_contours'] for f in all_features]
        mean_area_vals = [f['mean_area'] for f in all_features]
        std_area_vals = [f['std_area'] for f in all_features]
        mean_ar_vals = [f['mean_aspect_ratio'] for f in all_features]
        std_ar_vals = [f['std_aspect_ratio'] for f in all_features]

        layout_signature = {
            "avg_num_contours": safe_mean(num_contours_vals),
            "avg_mean_area": safe_mean(mean_area_vals),
            "avg_std_area": safe_mean(std_area_vals),
            "avg_mean_ar": safe_mean(mean_ar_vals),
            "avg_std_ar": safe_mean(std_ar_vals),
            "count_cards": len(all_features)
        }

        layout_signatures[layout] = layout_signature

    # Save the layout signatures
    with open(output_signature_json, 'w', encoding='utf-8') as f:
        json.dump(layout_signatures, f, indent=2, ensure_ascii=False)

    print(f"Layout signatures saved to {output_signature_json}.")

if __name__ == '__main__':
    main()
