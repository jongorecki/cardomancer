# Secrets Handling

Use `.env` + `python-dotenv`. Loaded once at server startup. Values read via `os.environ`.

---

## File layout

- `.env` — real secrets, gitignored, lives on local machine only
- `.env.example` — template with empty values + comments; **committed** so agents know what keys exist
- `.gitignore` — include `.env` (and `.env.*` except `.env.example`)

---

## `.env.example` contents

Create in Phase 0A. Agents implementing each source add their keys here.

```env
# Card Sorter — local secrets
# Copy to `.env` and fill in values. Never commit `.env`.

# ============================================================
# Moxfield (public-only integration for v1; push features deferred)
# ============================================================
# Not required for v1 (all pulls use public endpoints).
# Add only when enabling collection push:
# MOXFIELD_EMAIL=
# MOXFIELD_PASSWORD=

# ============================================================
# Discord notifications (optional, phase 4)
# ============================================================
# DISCORD_WEBHOOK_URL=

# ============================================================
# ntfy notifications (optional, phase 4)
# ============================================================
# NTFY_TOPIC=
# NTFY_SERVER=https://ntfy.sh

# ============================================================
# TCGPlayer API (deferred; only if we add their partner API)
# ============================================================
# TCGPLAYER_CLIENT_ID=
# TCGPLAYER_CLIENT_SECRET=

# ============================================================
# Refresh schedule overrides (optional)
# ============================================================
# Override cron expressions. Defaults are weekly / daily.
# ENRICHMENT_REFRESH_CRON_TAGGER=0 3 * * 0
# ENRICHMENT_REFRESH_CRON_EDHREC=0 4 * * 0
# ENRICHMENT_REFRESH_CRON_EDHTOP16=0 5 * * 0
# ENRICHMENT_REFRESH_CRON_SPELLBOOK=0 6 * * 0
# ENRICHMENT_REFRESH_CRON_BUYLIST_CK=0 7 * * *
# ENRICHMENT_REFRESH_CRON_PRICES=0 2 * * *
```

---

## Loading at startup

In `web_server.py`, before any other imports that might read env vars:

```python
from dotenv import load_dotenv
load_dotenv()  # loads .env from project root into os.environ
```

Add at top of the file, after the standard-library imports and before the Flask/SocketIO imports.

---

## Reading values

Every consumer reads via `os.environ.get(...)` with explicit defaults. Never crash on missing optional keys.

```python
# Good
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")  # None if unset
CRON_TAGGER = os.environ.get("ENRICHMENT_REFRESH_CRON_TAGGER", "0 3 * * 0")

# If a feature requires a key that's missing, degrade gracefully:
if not DISCORD_WEBHOOK_URL:
    logger.info("Discord notifications disabled (no webhook URL set)")
```

---

## What must NEVER go in `.env`

- The Marlin serial port (that's config.py; it's hardware, not a secret)
- Database paths
- Anything that affects behavior in a way users of the repo need to share
- File system paths

These belong in `config.py`. `.env` is **only** for per-deployment secrets.

---

## What goes in `.env` vs `config.py`

| Type | Location | Why |
|---|---|---|
| API keys, tokens, passwords | `.env` | Secret |
| Webhook URLs | `.env` | User-specific, often secret |
| Cron expressions | `.env` (override) + `config.py` (defaults) | User may want to tune |
| Serial port | `config.py` | Hardware constant |
| Hash DB paths | `config.py` | Project constant |
| ArUco IDs | `config.py` | Project constant |
| CROP_SIZE | `config.py` | **NEVER change** (per user memory) |

---

## Gitignore changes

If `.gitignore` doesn't exist (Phase 0A creates it along with `git init`), ensure it includes at minimum:

```
# Secrets
.env
.env.local

# Python
__pycache__/
*.pyc
*.pyo

# DBs (data, not code)
collection.db
collection.db-journal
collection.db-wal
collection.db-shm
enrichment.db
enrichment.db-journal
enrichment.db-wal
enrichment.db-shm

# Scan logs and debug outputs
logs/
scan_logs/
debug_crops/
debug_warps/
diag_*/
*.log
sim_debug.log
sim_start.log

# Large data files (user regenerates locally)
downloaded_cards/
card_hashes*.json
card_hashes*.npz
card_embeddings.npz
default-cards-*.json
AtomicCards.json

# Test artifacts
tests/probe_snapshots/*_20*.json  # keep only _pinned.json

# OS
Thumbs.db
.DS_Store

# IDE
.vscode/
.idea/
```

Decision points to verify with user before committing:
- Whether to track hash DBs (probably no — too large, regenerate locally)
- Whether to track `card_backup_reference.png` and calibration images (probably yes — small, reference data)
- Whether to track `default-cards-*.json` (probably no — huge, regenerate)

Agent should ask before final `.gitignore` decisions that affect tracked files.

---

## Credential rotation

User-facing rule: rotate by editing `.env` and restarting the server. No feature needs live credential rotation for v1.

---

## Logging rule

**Never log the contents of `.env` values.** If a webhook fires or an API call is made, log the endpoint name and result but never the token/URL/password. Applies to error messages too — redact before raising.

```python
# Bad
raise RuntimeError(f"Failed to post to {webhook_url}")

# Good
raise RuntimeError("Failed to post to Discord webhook (check DISCORD_WEBHOOK_URL)")
```

---

## Testing with fake secrets

Tests should use a `conftest.py` fixture that sets env vars to known fakes:

```python
# tests/conftest.py
import os, pytest

@pytest.fixture(autouse=True)
def fake_env(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://example.test/webhook")
    monkeypatch.setenv("NTFY_TOPIC", "test-topic")
    # unset anything that shouldn't be hit during tests:
    monkeypatch.delenv("MOXFIELD_EMAIL", raising=False)
    monkeypatch.delenv("MOXFIELD_PASSWORD", raising=False)
```

This ensures tests never accidentally hit real external services with real credentials.
