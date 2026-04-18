import json
from collections import Counter

# Load the JSON file
file_path = 'C:\\Users\\Jon\\Downloads\\default-cards-20241206100658.json'

try:
    with open(file_path, 'r', encoding='utf-8') as file:
        data = json.load(file)

    # Extract all "card_back_id" values
    card_back_ids = [item['card_back_id'] for item in data if 'card_back_id' in item]

    # Count the occurrences of each "card_back_id"
    counter = Counter(card_back_ids)

    # Find the most common "card_back_id"
    most_common = counter.most_common(1)

    if most_common:
        print(f"The most common card_back_id is '{most_common[0][0]}' with {most_common[0][1]} occurrences.")
    else:
        print("No card_back_id values found in the JSON file.")

except FileNotFoundError:
    print(f"File '{file_path}' not found.")
except json.JSONDecodeError:
    print("Failed to decode JSON. Please check the file format.")
except Exception as e:
    print(f"An error occurred: {e}")
