#!/usr/bin/env python3
"""
Build feature embedding database for card identification.

Uses DINOv2 (ViT-S/14, self-supervised) as a feature extractor.
Each card image → 384-dim L2-normalized embedding vector.
GPU-batched for speed.

DINOv2 self-supervised features are far better than ImageNet classification
features (MobileNetV2) for discriminating 50K+ similar fantasy art pieces.

Output: card_embeddings.npz
  - ids:        array of card ID strings, shape (N,)
  - embeddings: float32 array, shape (N, 384)
  - card_back:  float32 array, shape (1, 384) — card back reference

Usage:
    python build_embedding_db.py
"""

import os
import sys
import time
import glob
import numpy as np
import torch
from torchvision import transforms
from PIL import Image

# --- Config ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CARDS_DIR = os.path.join(SCRIPT_DIR, "downloaded_cards")
CARD_BACK_PATH = os.path.join(SCRIPT_DIR, "card_back_reference.png")
OUTPUT_PATH = os.path.join(SCRIPT_DIR, "card_embeddings.npz")

BATCH_SIZE = 64        # GPU batch size (ViT-S is lighter than ViT-B)
EMBED_DIM = 768        # DINOv2 ViT-B/14 feature dimension
IMG_SIZE = 518          # 518 = 37 * 14 patches — optimal for DINOv2's 14px patch size

# Art region crop coordinates (on a 745x1040 card image).
# Covers the main art area — no frame border, no text box.
# The art is the only discriminative part; frame/border is shared
# across thousands of cards of the same color.
ART_CROP = (30, 105, 715, 520)   # (left, top, right, bottom) = 685x415 art

# ImageNet normalization (DINOv2 was trained with it)
NORMALIZE = transforms.Normalize(
    mean=[0.485, 0.456, 0.406],
    std=[0.229, 0.224, 0.225],
)

PREPROCESS = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    NORMALIZE,
])


def build_model(device):
    """Load DINOv2 ViT-S/14 as a feature extractor."""
    model = torch.hub.load('facebookresearch/dinov2', 'dinov2_vitb14')
    model = model.to(device)
    model.eval()
    return model


def load_and_preprocess(img_path):
    """Load a card image, crop to art region, and preprocess."""
    try:
        img = Image.open(img_path).convert("RGB")
        img = img.crop(ART_CROP)
        return PREPROCESS(img)
    except Exception as e:
        print(f"  WARNING: Failed to load {img_path}: {e}")
        return None


@torch.no_grad()
def extract_batch(model, batch_tensor, device):
    """Extract L2-normalized embeddings for a batch of images."""
    batch_tensor = batch_tensor.to(device)
    features = model(batch_tensor)  # (B, 384) — cls_token output
    # L2-normalize
    features = torch.nn.functional.normalize(features, p=2, dim=1)
    return features.cpu().numpy()


def main():
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"Device: {device}")
    if device.type == "cuda":
        print(f"  GPU: {torch.cuda.get_device_name(0)}")
        print(f"  VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    print("Loading DINOv2 ViT-B/14...")
    model = build_model(device)
    print(f"  Feature dim: {EMBED_DIM}")
    print(f"  Input size: {IMG_SIZE}x{IMG_SIZE} (art crop {ART_CROP})")

    # --- Discover card images ---
    all_files = sorted(glob.glob(os.path.join(CARDS_DIR, "*.png")))
    print(f"Found {len(all_files)} card images in {CARDS_DIR}")

    if not all_files:
        print("ERROR: No card images found!")
        sys.exit(1)

    # Separate IDs, handle __back faces
    card_ids = []
    card_paths = []
    back_face_map = {}  # back_id -> canonical_id

    for path in all_files:
        fname = os.path.splitext(os.path.basename(path))[0]
        card_ids.append(fname)
        card_paths.append(path)
        if "__back" in fname:
            canonical_id = fname.replace("__back", "")
            back_face_map[fname] = canonical_id

    print(f"  {len(card_ids)} cards ({len(back_face_map)} back faces)")

    # --- Process card back reference ---
    card_back_embedding = None
    if os.path.exists(CARD_BACK_PATH):
        print("Processing card back reference...")
        img = Image.open(CARD_BACK_PATH).convert("RGB")
        # Crop to 745x1040 if needed (reference is 745x1043)
        if img.size[1] > 1040:
            img = img.crop((0, 0, 745, 1040))
        # Crop to art region like all other cards
        img = img.crop(ART_CROP)
        tensor = PREPROCESS(img).unsqueeze(0)
        card_back_embedding = extract_batch(model, tensor, device)
        print(f"  Card back embedding: {card_back_embedding.shape}")
    else:
        print(f"  WARNING: No card back reference at {CARD_BACK_PATH}")

    # --- Process all cards in batches ---
    n = len(card_ids)
    embeddings = np.empty((n, EMBED_DIM), dtype=np.float32)

    t0 = time.time()
    batch_tensors = []
    batch_indices = []
    failed = 0

    for i, path in enumerate(card_paths):
        tensor = load_and_preprocess(path)
        if tensor is None:
            # Fill with zeros for failed images
            embeddings[i] = np.zeros(EMBED_DIM, dtype=np.float32)
            failed += 1
            continue

        batch_tensors.append(tensor)
        batch_indices.append(i)

        # Process batch when full or at end
        if len(batch_tensors) >= BATCH_SIZE or i == n - 1:
            batch = torch.stack(batch_tensors)
            feats = extract_batch(model, batch, device)
            for j, idx in enumerate(batch_indices):
                embeddings[idx] = feats[j]

            batch_tensors = []
            batch_indices = []

            # Progress
            elapsed = time.time() - t0
            pct = (i + 1) / n * 100
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (n - i - 1) / rate if rate > 0 else 0
            print(f"\r  [{pct:5.1f}%] {i+1}/{n} cards, "
                  f"{rate:.0f} cards/s, ETA {eta:.0f}s", end="", flush=True)

    elapsed = time.time() - t0
    print(f"\n  Done: {n} cards in {elapsed:.1f}s "
          f"({n/elapsed:.0f} cards/s, {failed} failed)")

    # --- Save ---
    print(f"Saving to {OUTPUT_PATH}...")
    save_dict = {
        "ids": np.array(card_ids, dtype=object),
        "embeddings": embeddings,
    }
    if card_back_embedding is not None:
        save_dict["card_back"] = card_back_embedding

    np.savez(OUTPUT_PATH, **save_dict)

    file_size = os.path.getsize(OUTPUT_PATH) / 1e6
    print(f"  Saved: {file_size:.1f} MB "
          f"({n} cards × {EMBED_DIM} dims)")

    # --- Quick self-test ---
    print("\nSelf-test: checking first 5 cards match themselves...")
    test_n = min(5, n)
    for i in range(test_n):
        query = embeddings[i:i+1]  # (1, 384)
        sims = (embeddings @ query.T).squeeze()  # (N,)
        best_idx = np.argmax(sims)
        best_sim = sims[best_idx]
        match = "OK" if best_idx == i else f"MISMATCH (got {best_idx})"
        print(f"  {card_ids[i][:20]}... -> sim={best_sim:.4f} {match}")

    print("\nDone!")


if __name__ == "__main__":
    main()
