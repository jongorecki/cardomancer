import json
import os
import requests
import time

def download_card_images(cards, output_dir, image_size='png', calls_per_second=10):
    """
    Downloads card images from the Scryfall card data provided in the `cards` list,
    with a rate limit on calls per second.
    """
    os.makedirs(output_dir, exist_ok=True)
    delay = 1.0 / calls_per_second

    total_cards = len(cards)
    for idx, card in enumerate(cards, start=1):
        card_name = card.get('name', 'Unknown')
        card_id = card.get('id', card_name.replace(' ', '_'))

        print(f"\nProcessing card {idx}/{total_cards}: {card_name} (ID: {card_id})")

        # Check for image_uris
        if 'image_uris' not in card:
            print(f"  [SKIP] No image_uris found for card {card_name}")
            continue

        # Check if requested image size is available
        image_uri = card['image_uris'].get(image_size)
        if not image_uri:
            print(f"  [SKIP] No '{image_size}' image available for card {card_name}")
            continue

        filename = f"{card_id}.png"
        output_path = os.path.join(output_dir, filename)

        # Check if the file already exists to avoid re-downloading
        if os.path.exists(output_path):
            print(f"  [SKIP] Image for {card_name} already exists at {output_path}")
            continue

        # Download the image
        print(f"  [DOWNLOAD] Downloading image from {image_uri}")
        try:
            response = requests.get(image_uri, stream=True)
            response.raise_for_status()
            with open(output_path, 'wb') as img_file:
                for chunk in response.iter_content(chunk_size=8192):
                    img_file.write(chunk)
            print(f"  [SUCCESS] Downloaded {card_name} to {output_path}")
        except Exception as e:
            print(f"  [ERROR] Failed to download {card_name}: {e}")
            continue

        # Rate limiting
        time.sleep(delay)

    print("\nAll downloads attempted. Check the output directory for results.")


if __name__ == "__main__":
    # Adjust paths as necessary
    json_file_path = r"C:\Users\Jon\Downloads\default-cards-20241206100658.json"
    output_dir = "downloaded_cards"
    image_size = 'png'
    calls_per_second = 10

    print(f"Loading card data from: {json_file_path}")
    with open(json_file_path, 'r', encoding='utf-8') as f:
        all_cards = json.load(f)
    print(f"Found {len(all_cards)} cards in the JSON file.")

    # Ask the user if they want to prioritize any sets first
    sets_input = input("Enter comma-separated set codes to download first (or leave blank to skip): ").strip()
    if sets_input:
        desired_sets = [s.strip().lower() for s in sets_input.split(',')]
    else:
        desired_sets = []

    # Separate cards into desired sets and the rest
    if desired_sets:
        priority_cards = [c for c in all_cards if c.get('set', '').lower() in desired_sets]
        remaining_cards = [c for c in all_cards if c.get('set', '').lower() not in desired_sets]

        if priority_cards:
            print(f"\nDownloading cards from sets: {', '.join(desired_sets)}")
            download_card_images(priority_cards, output_dir, image_size, calls_per_second)
        else:
            print("\nNo cards found from the specified sets.")

        # Now download the remaining cards
        if remaining_cards:
            print("\nDownloading remaining cards...")
            download_card_images(remaining_cards, output_dir, image_size, calls_per_second)
    else:
        # No sets specified, just download all cards
        download_card_images(all_cards, output_dir, image_size, calls_per_second)
