# Backup Discipline

Both mechanisms: `git init` for real version control AND `backup/` snapshots before destructive edits. Belt and suspenders.

---

## Phase 0A: initialize git

First commit before any code changes. Agent working on Phase 0A does this:

```bash
cd D:/Card_Sorter/Scripts

git init
# Confirm defaults
git config --local core.autocrlf true       # Windows line endings
git config --local core.ignorecase true

# Create .gitignore (contents in 08_secrets_handling.md)
# ... write .gitignore ...

# Stage and commit everything currently in the project
git add .
git commit -m "initial commit: import working sorter state pre-enrichment work"
```

This snapshot becomes the baseline. Every implementation agent branches from here.

---

## Branch naming

Feature branches follow this pattern:

- `phase0a-foundation`
- `phase0b-preset-ui`
- `phase0b-probes`
- `phase1-tagger`
- `phase1-edhrec`
- `phase1-edhtop16`
- `phase1-spellbook`
- `phase2-locator`
- `phase2-filters`
- `phase2-cull`
- `phase3-buylist`
- `phase3-moxfield`
- `phase4-live-info`
- `phase4-gallery`
- `phase4-wizard`
- `phase4-wishlist`

Merge to `main` when acceptance criteria pass.

---

## Commit frequency

Agents commit frequently within their feature branch:

- After any significant function is working
- After tests pass
- Before attempting any risky refactor
- At end of each agent session (so the next session can pick up)

Commit messages:
- Imperative mood: `add Tagger GraphQL probe`, not `added` or `adding`.
- First line < 60 chars.
- Optional body for context if the change is non-obvious.
- No trailers required.

Example:
```
add TaggerSource.refresh() with hierarchy rollup

Uses Scryfall search API as primary path; GraphQL for tag catalog only.
Writes to tags/art_tags/tag_catalog tables in enrichment.db.
Probe pinned at tests/probe_snapshots/tagger_pinned.json.
```

---

## Pre-edit backup snapshots (additional safety net)

Agents must snapshot files to `backup/` before significant edits to **existing** files. This is redundant with git but protects against:

- Accidental git commands (reset --hard, force-push, etc.)
- Agent hitting `Write` on an existing file without reading it first
- Multiple agents touching the same file
- The user wanting to quickly diff "what did the agent change" without running `git`

### When to snapshot

Snapshot before editing any file in the "modify with caution" list from `04_scope_fences.md`:
- `web_server.py`
- `web_worker.py`
- `web_database.py`
- `collection_db.py`
- `static/app.js`
- `templates/index.html`
- `static/style.css`
- `query_parser.py`
- `config.py`

NOT needed for files the agent creates.

### Snapshot procedure

```bash
# Example: before editing web_server.py
TS=$(date +%Y%m%d_%H%M%S)
mkdir -p "backup/${TS}/"
cp web_server.py "backup/${TS}/web_server.py"
```

Or in Python:

```python
import shutil
from datetime import datetime
from pathlib import Path

def snapshot(file_path: str) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path("backup") / ts
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / Path(file_path).name
    shutil.copy2(file_path, dest)
    return str(dest)
```

Log the snapshot path in the agent's work summary so the user can find it later.

---

## Backup directory hygiene

`backup/` can grow large. Periodic cleanup (user-initiated, not automated):

- Snapshots older than 30 days: user can delete freely
- Snapshots tied to merged git commits: redundant with git, user can delete
- Snapshots tied to in-flight or abandoned work: keep until resolved

Agents should NOT auto-clean `backup/`. Leave it to the user.

---

## Restoration procedure

### From git
```bash
# See what changed on a branch
git diff main..phase1-tagger

# Revert a specific file to main's version
git checkout main -- web_server.py

# Abandon a branch entirely
git checkout main
git branch -D phase1-tagger
```

### From backup/
```bash
# List snapshots for a file
ls backup/*/web_server.py

# Restore a specific snapshot
cp backup/20260418_143012/web_server.py web_server.py
```

---

## DB backups

Existing `collection.db` and new `enrichment.db` should be backed up before any schema migration:

```python
# In enrichment_db.py, before running migrations:
import shutil
from datetime import datetime
if os.path.exists("enrichment.db"):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2("enrichment.db", f"backup/dbs/enrichment.db.{ts}")
```

Keep DB backups in `backup/dbs/` (subdirectory so they don't get mixed with code snapshots).

---

## Pre-release checkpoint

Before any agent marks a feature complete and requests merge:

1. `git status` — clean working tree
2. `git log main..HEAD --oneline` — meaningful commit history
3. All acceptance criteria for the feature passing
4. Probe script (if applicable) green
5. Test suite passing
6. A final snapshot of modified existing files in `backup/`

Then the user can review the diff and merge.

---

## Rules about destructive git operations

**Agents must NOT run without explicit user approval:**
- `git push --force` (anywhere, for any reason)
- `git reset --hard`
- `git clean -fd`
- `git branch -D` (on any branch other than their own abandoned feature branch)
- `git rebase -i`
- `git filter-branch` or `git filter-repo`
- `rm -rf` on anything git-tracked

If an agent encounters a git state it doesn't understand, it stops and asks.

---

## Remote repo (deferred)

v1: local git only. No GitHub/GitLab push.

When/if the user adds a remote:
- Add `.gitignore` entries for anything secret (already done via `08_secrets_handling.md`)
- Verify `.env` is ignored before first push
- First push should be `--set-upstream`, not `--force`
- Branch protection can be added on main if multiple agents will push directly
