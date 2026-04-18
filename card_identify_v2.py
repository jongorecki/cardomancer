#!/usr/bin/env python3
"""
Card identification via DINOv2 feature embeddings.

Extracts a 384-dim feature vector from the full card image using DINOv2
(ViT-S/14, self-supervised), then finds the closest match in the
precomputed embedding database via cosine similarity.
Tries both upright and 180-degree rotated orientations.

DINOv2 self-supervised features are far more discriminative than ImageNet
classification features for distinguishing 50K+ similar fantasy art pieces.

Typical performance: ~15-25ms per card.

Requires: card_embeddings.npz (built by build_embedding_db.py)
"""

import os
import time
import cv2
import numpy as np
import torch
from torchvision import transforms
from PIL import Image

# --- Config ---
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
EMBEDDING_DB_PATH = os.path.join(SCRIPT_DIR, "card_embeddings.npz")

# Match threshold: cosine similarity (1.0 = identical, 0.0 = orthogonal)
MATCH_THRESHOLD = 0.4
CARD_BACK_THRESHOLD = 0.6

IMG_SIZE = 518          # 518 = 37 * 14 patches — optimal for DINOv2

# Art region crop coordinates (on a 745x1040 card image).
# Must match build_embedding_db.py exactly.
ART_CROP = (30, 105, 715, 520)   # (left, top, right, bottom) = 685x415 art

NORMALIZE = transforms.Normalize(
    mean=[0.485, 0.456, 0.406],
    std=[0.229, 0.224, 0.225],
)

PREPROCESS = transforms.Compose([
    transforms.Resize((IMG_SIZE, IMG_SIZE)),
    transforms.ToTensor(),
    NORMALIZE,
])

# --- Module state ---
_model = None
_device = None
_card_ids = []              # list of card ID strings
_embeddings = None          # numpy float32 (N, 384)
_card_back_embedding = None # numpy float32 (1, 384)
_db_loaded = False


def _load_model():
    """Load DINOv2 ViT-S/14 feature extractor."""
    global _model, _device
    if _model is not None:
        return

    _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    _model = torch.hub.load('facebookresearch/dinov2', 'dinov2_vitb14')
    _model = _model.to(_device)
    _model.eval()
    print(f"[card_identify_v2] DINOv2 ViT-B/14 loaded on {_device}")


def _load_db():
    """Load embedding database."""
    global _card_ids, _embeddings, _card_back_embedding, _db_loaded

    if _db_loaded:
        return
    _db_loaded = True

    if not os.path.exists(EMBEDDING_DB_PATH):
        print(f"[card_identify_v2] WARNING: No embedding DB at "
              f"{EMBEDDING_DB_PATH}")
        return

    t0 = time.time()
    data = np.load(EMBEDDING_DB_PATH, allow_pickle=True)

    _card_ids = list(data["ids"])
    _embeddings = data["embeddings"].astype(np.float32)

    if "card_back" in data:
        _card_back_embedding = data["card_back"].astype(np.float32)
        print("[card_identify_v2] Card back reference loaded")

    elapsed = time.time() - t0
    print(f"[card_identify_v2] Loaded {len(_card_ids)} embeddings "
          f"({elapsed:.2f}s, {_embeddings.shape[1]}-dim)")


# Load on import
_load_model()
_load_db()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_pil_full(card_img):
    """Convert BGR numpy array to full PIL RGB image (no crop)."""
    if isinstance(card_img, np.ndarray):
        return Image.fromarray(cv2.cvtColor(card_img, cv2.COLOR_BGR2RGB))
    return card_img.convert("RGB")


def _crop_art(pil_full):
    """Crop a full card PIL image to the art region."""
    return pil_full.crop(ART_CROP)


@torch.no_grad()
def _extract_embedding(pil_img):
    """Extract L2-normalized embedding from a PIL image (already cropped)."""
    tensor = PREPROCESS(pil_img).unsqueeze(0).to(_device)
    features = _model(tensor)  # (1, 384) — cls_token
    features = torch.nn.functional.normalize(features, p=2, dim=1)
    return features.cpu().numpy()  # (1, 384)


def _match(query_embedding):
    """
    Find best match via cosine similarity.

    :param query_embedding: (1, 384) numpy array
    :return: (best_idx, best_sim, all_sims)
    """
    # Cosine similarity = dot product (both are L2-normalized)
    sims = (_embeddings @ query_embedding.T).squeeze()  # (N,)
    best_idx = int(np.argmax(sims))
    best_sim = float(sims[best_idx])
    return best_idx, best_sim, sims


# ---------------------------------------------------------------------------
# Card back detection
# ---------------------------------------------------------------------------

def is_card_back(card_img):
    """
    Check if the card image is a card back.

    :param card_img: 745x1040 BGR numpy array or PIL Image
    :return: (is_back: bool, similarity: float)
    """
    if _card_back_embedding is None:
        return False, 0.0

    pil_full = _to_pil_full(card_img)

    # Upright: crop art region, extract embedding
    emb = _extract_embedding(_crop_art(pil_full))

    # Rotated: rotate full card FIRST, then crop art region
    pil_rot = pil_full.rotate(180)
    emb_rot = _extract_embedding(_crop_art(pil_rot))

    sim_up = float(emb @ _card_back_embedding.T)
    sim_rot = float(emb_rot @ _card_back_embedding.T)
    sim = max(sim_up, sim_rot)

    return sim >= CARD_BACK_THRESHOLD, sim


# ---------------------------------------------------------------------------
# Card identification
# ---------------------------------------------------------------------------

def identify_card(card_img, threshold=None):
    """
    Identify a card via DINOv2 feature embedding matching.

    :param card_img: 745x1040 BGR numpy array or PIL Image
    :param threshold: Min cosine similarity for match (default: MATCH_THRESHOLD)
    :return: (card_id, similarity, was_rotated, all_results)
             card_id is None if no match above threshold.
             all_results is sorted [(card_id, similarity)] descending.
    """
    if threshold is None:
        threshold = MATCH_THRESHOLD

    if _embeddings is None or len(_card_ids) == 0:
        return None, 0.0, False, []

    pil_full = _to_pil_full(card_img)

    # Try upright: crop art region, extract, match
    emb_up = _extract_embedding(_crop_art(pil_full))
    idx_up, sim_up, sims_up = _match(emb_up)

    # Try 180-degree rotated: rotate full card FIRST, then crop art
    pil_rot = pil_full.rotate(180)
    emb_rot = _extract_embedding(_crop_art(pil_rot))
    idx_rot, sim_rot, sims_rot = _match(emb_rot)

    # Pick the better match
    if sim_up >= sim_rot:
        best_idx, best_sim = idx_up, sim_up
        best_sims = sims_up
        was_rotated = False
    else:
        best_idx, best_sim = idx_rot, sim_rot
        best_sims = sims_rot
        was_rotated = True

    # Build sorted results
    all_results = sorted(
        zip(_card_ids, best_sims.tolist()),
        key=lambda x: x[1],
        reverse=True,  # highest similarity first
    )

    best_id = _card_ids[best_idx]

    if best_sim >= threshold:
        return best_id, best_sim, was_rotated, all_results
    else:
        return None, best_sim, was_rotated, all_results
