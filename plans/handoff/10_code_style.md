# Code Style

Match existing conventions. No new formatter or linter. When in doubt, read the surrounding file and mirror its patterns.

---

## Python

### Layout & imports

```python
# module_name.py
# ---------------------------------------------------------------------------
# Brief description of what this module does.
# More detail if the WHY is non-obvious.
# ---------------------------------------------------------------------------

# Standard library first
import os
import json
import sqlite3
from datetime import datetime

# Third-party next
import httpx
from flask import jsonify

# Local imports last
from config import SCRIPT_DIR
from web_enrichment.base import EnrichmentSource
```

Follow exactly the header comment style used in `collection_db.py`, `web_database.py`, `config.py`. Every new module file starts with a bang-delimited description.

### Naming

- Functions and variables: `snake_case`
- Classes: `PascalCase`
- Constants: `UPPER_SNAKE_CASE` (at module top)
- Private helpers: `_leading_underscore`

### Type hints

Existing code is inconsistent on type hints — some files have them, some don't. **New code uses type hints.** No exceptions for new files.

```python
def refresh(self, emit: Optional[Callable[[str, dict], None]] = None,
            full: bool = False) -> RefreshResult:
    ...
```

Use `from __future__ import annotations` at the top of new files for forward references.

### Docstrings

Short, purpose-focused. Match the style in `collection_db.py`:

```python
def start_session(conn, sort_mode=None, config_name=None, bin_count=None):
    """Start a new sorting session. Returns the session id."""
```

NOT Sphinx/NumPy style. Keep to 1–2 lines unless absolutely necessary. Avoid parameter-by-parameter docs unless behavior is non-obvious.

### Comments

**Default: no comments.** Only add when WHY is non-obvious (from main CLAUDE.md instructions).

Good uses, per existing code:
- `config.py` comments explaining why `CROP_SIZE = 745` matches Scryfall PNG size
- `config.py` comment on CAMERA_X_OFFSET sign convention

Bad (don't do):
- Explaining what code does when the code is self-documenting
- References to the current task or fix
- "Removed X" stubs

### Error handling

- Internal code: trust. Don't wrap every call in try/except.
- External boundaries (HTTP, file IO, DB): handle explicitly.
- Raise with clear messages: `raise ValueError(f"Unknown source: {name}")` not `raise Exception("bad input")`.
- Log before raising when the error crosses a subsystem boundary.
- Never swallow exceptions silently.

### Logging

Use the existing logging setup from `web_server.py`:

```python
import logging
logger = logging.getLogger(__name__)

logger.info("Starting Tagger refresh")
logger.warning("Tagger coverage below threshold: %.1f%%", pct)
logger.error("Probe failed for %s: %s", source_name, err)
```

Match the `logger = logging.getLogger(__name__)` pattern. Don't use `print()` in new code (existing code has some print statements; don't add more).

### SQL

Match `collection_db.py` patterns:
- `executescript` for schema creation
- Parameterized queries always (`?` placeholders, never string concatenation)
- `sqlite3.Row` factory for column access by name
- WAL mode + foreign keys enabled on connection

```python
row = conn.execute(
    "SELECT * FROM staples WHERE oracle_id = ? AND tier = ?",
    (oracle_id, tier)
).fetchone()
```

### Strings

- Prefer f-strings for interpolation: `f"Refreshed {n} tags"`
- Use `.format()` only when the template is reused
- Logging: prefer `%s`-style (`logger.info("x=%s", x)`) — it defers formatting to only when the log level is enabled

### Line length

Existing code is loose on this. Target 100 chars; no hard rule. Don't wrap at 80 just for the sake of it.

### Empty lines

- Two blank lines between top-level functions and classes
- One blank line between methods within a class
- No blank lines at end of file
- One blank line at end of each function (matches existing style)

---

## JavaScript

### File organization

All JS in `static/app.js`. Don't split into modules (no build step). Add new functionality as new sections, with a comment header matching existing style:

```javascript
// ==========================================================================
// Live Card Info Overlay (Phase 4)
// ==========================================================================

function renderLiveCardInfo(data) {
    // ...
}
```

### Naming

- Functions: `camelCase`
- Variables: `camelCase`
- Constants: `SCREAMING_SNAKE_CASE` (rarely used)
- DOM IDs: `kebab-case` (match existing `#tab-dashboard`, `#btn-estop`, etc.)

### DOM manipulation

Existing code uses vanilla DOM + Bootstrap. Match that.

```javascript
document.getElementById('some-id').textContent = value;
document.querySelector('.some-class').classList.add('active');
```

No jQuery (not loaded). No modern framework. No fetch wrappers beyond what `app.js` already has (`apiPost`, `apiGet` helpers — extend these rather than invent new ones).

### SocketIO handlers

Register in the existing block that handles other events:

```javascript
socket.on('enrichment_refresh_progress', (data) => {
    updateEnrichmentProgress(data);
});
```

### Errors

Alert-style popups exist (`alert(...)`). Don't abuse — use console.error for dev-visible errors, and toast/badge UI for user-visible problems.

---

## CSS

### File

Single file: `static/style.css`. Append new classes at the end in a clearly-labeled section:

```css
/* =========================================================================
 * Enrichment source cards (Phase 0A)
 * ========================================================================= */

.source-card {
    background: var(--bg-card);
    border: 1px solid var(--border-card);
    /* ... */
}
```

### Variables

Use existing CSS variables from `:root` and the `[data-theme="light"]` override block. Never hardcode colors:

```css
/* Good */
color: var(--text-primary);
background: var(--bg-card);

/* Bad */
color: #e8eaf0;
```

If a new color is needed, add it as a variable in BOTH `:root` and `[data-theme="light"]`.

### Selectors

- Class-based, one level deep when possible
- Avoid `!important` unless overriding Bootstrap and documented in a comment
- Prefer Bootstrap utilities when available (e.g., `d-flex`, `mt-3`)

---

## HTML (Jinja templates)

### `templates/index.html`

- 4-space indent (match existing)
- Bootstrap classes everywhere possible
- Custom CSS classes for non-Bootstrap styling
- Inline event handlers are fine (`onclick="..."`) — match existing style

### New components

Place in the appropriate tab block. Use Bootstrap card/modal/offcanvas patterns already in use.

---

## File size limits

- Python module: aim for < 1000 lines. Split into sub-modules if needed.
- `app.js`: already 4200+ lines; adding sections is fine, but keep each feature's code tightly scoped so future extraction is possible.
- `index.html`: already 982 lines; new tabs/modals add to it. Ignore size for now.

---

## Testing style

See `11_test_framework.md`. Match `tests/test_collection_db.py` patterns: `unittest.TestCase` subclasses, `setUp`/`tearDown`, `tempfile`-based fixtures.

---

## "Don't add" list

Matching main CLAUDE.md:

- No emojis in code or comments unless explicitly requested
- No documentation files unless explicitly requested
- No new comments explaining WHAT the code does
- No references to "current task" / "fix" / "added for feature X" in comments
- No backwards-compatibility shims for features not yet released
- No TODO/FIXME as a substitute for doing the work — do it or open a task

---

## Pre-commit checklist (human or agent)

Before each commit:

1. File has module-level description comment at top (for new files)
2. Imports sorted (stdlib, third-party, local)
3. All functions type-annotated (new code)
4. No stray `print()` / `console.log()`
5. No hardcoded colors in CSS
6. Tests added/updated for new behavior
7. `pytest tests/` passes
