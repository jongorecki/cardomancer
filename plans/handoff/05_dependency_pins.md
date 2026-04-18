# Dependency Pins

Exact libraries + versions. Pin in `requirements.txt`. Match existing sync/Flask patterns; no async in Flask routes.

---

## Existing (do not change)

From inspection of `web_server.py` imports:

- `Flask` — web framework
- `flask_socketio` — SocketIO support
- `cv2` (opencv-python) — used by camera/detection
- Standard library: `os`, `io`, `json`, `time`, `atexit`, `signal`, `logging`, `sys`, `traceback`

---

## New dependencies for this work

### HTTP + networking
```
httpx==0.27.*
```
**Why:** modern, sync+async capable, cleaner API than `requests`. Use sync mode everywhere inside Flask routes (matching existing code style). Timeout defaults to 30s; override per-source as needed.

### GraphQL
```
gql[requests]==3.5.*
```
**Why:** needed for `edhtop16` and (probably) `tagger.scryfall.com`. The `[requests]` extra gives a sync transport. If Tagger turns out to need auth cookies, wrap with a custom `httpx`-based transport instead — the main `gql` library is optional, document-it-yourself fine.

### Scheduling
```
APScheduler==3.10.*
```
**Why:** simple in-process cron. Matches Flask lifecycle. Avoids adding a separate process. Use `BackgroundScheduler` with `ThreadPoolExecutor`.

### Validation
```
pydantic==2.7.*
```
**Why:** schema snapshot tests and source response models. V2 is current; faster and better typed than v1. Use `pydantic.BaseModel` for all source response types.

### Config / secrets
```
python-dotenv==1.0.*
```
**Why:** load `.env` at server startup. See `08_secrets_handling.md`.

### Webhooks (optional; phase 4)
```
# only if we ship Discord/ntfy notifications
# no new dep needed — httpx handles both
```

### HTML parsing (CardKingdom buylist scraping only)
```
beautifulsoup4==4.12.*
```
**Why:** CK publishes buylist as HTML. Scrape only what we need; keep to a single file `web_integrations/buylist_ck.py`.

---

## What NOT to add

- **`requests`** — redundant with `httpx`
- **`aiohttp`** — Flask routes are sync
- **`sqlalchemy`** — existing code uses raw `sqlite3` with `sqlite3.Row` factory; match that
- **`alembic`** — migrations are simple enough for handwritten scripts
- **`celery` / `rq`** — APScheduler is sufficient; no Redis overhead
- **`selenium` / `playwright`** — if a source requires a real browser, stop and ask the user
- **Any ORM** — raw SQL matches existing `collection_db.py` style
- **`fastapi`** — we're on Flask; do not switch frameworks
- **`jinja2`** additions — Flask already bundles Jinja2; use what's there

---

## Version philosophy

- Pin to minor versions (`==0.27.*`) so security patches roll in automatically.
- Major version bumps must be explicit and flagged in a PR (or backup commit in our case).
- Do not upgrade existing pinned deps as part of this work; file a separate task if needed.

---

## Installation procedure

Assume a Windows environment with existing venv. Agent should:

1. Check existing `requirements.txt`; if missing, create it seeded with observed imports first.
2. Append new deps from this doc.
3. `pip install -r requirements.txt` in the project venv.
4. Verify imports from a one-liner `python -c "import httpx, gql, apscheduler, pydantic, dotenv"`.

Do not run `pip install --upgrade` on any existing package.

---

## Frontend dependencies

Existing: **Bootstrap 5.3.3** via CDN in `templates/index.html`. Do not add a build step or bundler.

If new JS functionality is needed:
- Write vanilla JS in `static/app.js` — match existing code style
- No frontend framework (no React/Vue/Svelte)
- No npm, no package.json, no node_modules
- If a utility is truly needed (e.g., charts for session analytics), load from CDN — prefer Chart.js for charts since it's small and has no deps

Chart library (only when session analytics ships):
```html
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
```

---

## Test-time dependencies

```
pytest==8.*
pytest-mock==3.*
```

That's it. No coverage tool yet (add later if needed). Use `unittest.TestCase` style for parity with existing tests, with pytest as the runner.

---

## Environment requirements

- Python 3.10+ (f-strings, `match` statement, typing features are fair game)
- Windows 11 (primary dev environment per CLAUDE.md env block)
- No Docker/containers required or expected
