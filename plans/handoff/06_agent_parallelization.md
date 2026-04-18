# Agent Parallelization Map

Which tasks can run in parallel without merge conflicts; which must be sequential. Assumes `git init` has been run (see `09_backup_discipline.md`) so agents work on feature branches.

---

## Dependency DAG

```
                    ┌─────────────────────────────┐
                    │ Phase 0A: Foundation        │
                    │ (sequential, 1 agent)       │
                    │                             │
                    │  - enrichment.db schema     │
                    │  - shared interfaces        │
                    │  - .env loader              │
                    │  - APScheduler wiring       │
                    │  - tab consolidation        │
                    │  - Motion Preview hide      │
                    └──────────────┬──────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          │                        │                        │
 ┌────────▼────────┐      ┌────────▼────────┐      ┌────────▼────────┐
 │ Phase 0B:       │      │ Phase 0B:       │      │ Phase 0B:       │
 │ Unified Preset  │      │ All probe       │      │ Code style /    │
 │ UI              │      │ scripts         │      │ test infra      │
 │ (1 agent)       │      │ (1 agent)       │      │ scaffold        │
 └────────┬────────┘      └────────┬────────┘      └─────────────────┘
          │                        │
          │                        │
          │               ┌────────┴──────────────────────┐
          │               │                               │
          │   ┌───────────▼──────┐   ┌──────────▼────────┐  ┌──────────▼────────┐
          │   │ Phase 1: Tagger  │   │ Phase 1: EDHREC   │  │ Phase 1: Spellbook│
          │   │ cache            │   │ staples + salt +  │  │ combos            │
          │   │ (1 agent)        │   │ themes            │  │ (1 agent)         │
          │   │                  │   │ (1 agent)         │  │                   │
          │   └────────┬─────────┘   └──────────┬────────┘  └──────────┬────────┘
          │            │                        │                       │
          │            │              ┌─────────▼────────┐              │
          │            │              │ Phase 1:         │              │
          │            │              │ edhtop16 cEDH    │              │
          │            │              │ staples          │              │
          │            │              │ (can run in      │              │
          │            │              │  parallel with   │              │
          │            │              │  EDHREC above)   │              │
          │            │              └─────────┬────────┘              │
          │            │                        │                       │
          └────────────┴────────┬───────────────┴───────────────────────┘
                                │
                ┌───────────────┼───────────────┐
                │               │               │
        ┌───────▼──────┐ ┌──────▼─────┐ ┌──────▼──────┐
        │ Phase 2:     │ │ Phase 2:   │ │ Phase 2:    │
        │ Locator +    │ │ Tag/stap.  │ │ Cull view   │
        │ divider/box  │ │ filters in │ │ (1 agent)   │
        │ (1 agent)    │ │ Collection │ │             │
        │              │ │ (1 agent)  │ │             │
        └──────────────┘ └────────────┘ └──────┬──────┘
                                               │
                                         ┌─────▼──────┐
                                         │ Phase 3:   │
                                         │ Buylist    │
                                         │ (1 agent)  │
                                         └─────┬──────┘
                                               │
                                       ┌───────▼──────┐
                                       │ Phase 3:     │
                                       │ Moxfield     │
                                       │ pull (deck + │
                                       │ binder)      │
                                       │ (1 agent)    │
                                       └───────┬──────┘
                                               │
              ┌────────────────────────────────┼───────────────┐
              │                                │               │
      ┌───────▼──────────┐         ┌──────────▼─────┐  ┌──────▼───────┐
      │ Phase 4:         │         │ Phase 4:       │  │ Phase 4:     │
      │ Live card info   │         │ Session gall.  │  │ Calibration  │
      │ overlay          │         │ + analytics    │  │ wizard       │
      │ (1 agent)        │         │ (1 agent)      │  │ (1 agent)    │
      └──────────────────┘         └────────────────┘  └──────────────┘
                                                              │
                                                    ┌─────────▼──────┐
                                                    │ Phase 4:       │
                                                    │ Wishlist       │
                                                    │ priority +     │
                                                    │ notifications  │
                                                    │ (1 agent)      │
                                                    └────────────────┘
```

---

## Phase breakdown with parallelization notes

### Phase 0A: Foundation (MUST be sequential, single agent)

Blocks everything else. Cannot be parallelized.

1. Initialize git repo + first commit
2. Create `enrichment.db` schema + migrations (`enrichment_db.py`)
3. Define shared interfaces (`EnrichmentRepo` class signatures, emit callback contract)
4. Add `python-dotenv` + `.env.example`
5. Wire APScheduler into `web_server.py`
6. Tab consolidation in `index.html` (4 tabs, Calibration → modal, Database → subsection)
7. Hide Motion Preview tab
8. Scaffold test directories (`tests/enrichment/`, `tests/probes/`, `tests/fixtures/`, `tests/probe_snapshots/`)

**Output:** branch `phase0a-foundation` merged to main. Everything downstream forks from here.

**Conflict surface:** writes/modifies `web_server.py`, `index.html`, `app.js`, `style.css`, `requirements.txt`, `config.py`. Nothing else can safely touch these during Phase 0A.

---

### Phase 0B: Parallel foundation (3 agents max, after 0A)

These can run in parallel because they touch disjoint files:

- **0B-1 Unified Preset UI** — touches `app.js` (new view logic), `index.html` (Sort Session tab), new endpoints in `web_server.py`, possibly new `preset_store.py`. Primary conflict risk: `app.js`. Coordinate with agent 0B-3.
- **0B-2 Probe scripts** — touches only new files under `probes/` and `tests/probe_snapshots/`. No conflicts.
- **0B-3 Test infra scaffold** — touches `tests/` structure, `conftest.py`, maybe minor adds to existing test files. No conflicts with 0B-1 if no shared test files.

**After 0B:** merge all three. Phase 1 can begin.

---

### Phase 1: Annotation pulls (3–4 agents parallel, after 0B)

Each source is a separate file; they write to different tables in enrichment.db. Can run entirely in parallel:

- **1-Tagger** — `probes/probe_tagger.py`, `web_enrichment/tagger.py`, writes to `tags`/`art_tags`/`tag_catalog`
- **1-EDHREC** — `web_enrichment/edhrec.py`, writes to `staples`(universal/archetype)/`salt_scores`/`themes`
- **1-edhtop16** — `web_enrichment/edhtop16.py`, writes to `staples`(cedh)
- **1-Spellbook** — `web_enrichment/spellbook.py`, writes to `combos`/`combo_membership`

All four agents follow the same interface contract (see `07_shared_interfaces.md`) so they slot into the refresh scheduler uniformly.

**Merge order:** any order. All four write to separate tables. The only shared file is `web_server.py` (registering the refresh endpoint per source); have the 0A agent pre-scaffold the endpoints so Phase 1 agents only need to implement the manager class.

**Conflict avoidance trick:** have 0A create stub functions `refresh_tagger()`, `refresh_edhrec()`, etc., that just `raise NotImplementedError`. Phase 1 agents replace the stubs — clean git history, no merge conflicts on endpoint registration.

---

### Phase 2: Collection tooling (3 agents, mostly parallel, after Phase 1)

- **2-Locator/divider** — `web_locator.py`, `web_storage.py`, new tables in `collection_db.py` (`storage_locations`, `storage_sessions`), new endpoints, Collection tab Locator sub-view
- **2-Filters** — Collection tab Inventory sub-view enhancements (filter sidebar, enrichment query support in existing search), enrichment-aware endpoint
- **2-Cull view** — cull-candidates endpoint + cull-to-preset endpoint; builds on filter backend from 2-Filters

**Dependency:** 2-Cull needs 2-Filters' query backend. Run 2-Filters first (or build the query backend in 0B, then 2-Cull can run parallel with 2-Filters' UI work).

**Conflict surface:** all three touch the Collection tab UI and `app.js`. Recommend splitting:
- 2-Locator writes a new Locator sub-view (isolated)
- 2-Filters modifies Inventory sub-view (isolated)
- 2-Cull adds cull preset to filter sidebar (touches Filters; do 2-Cull after 2-Filters merges)

---

### Phase 3: Integrations (2 agents, sequential)

- **3-Buylist** — CK scraper, daily refresh, `buylists` table. Independent.
- **3-Moxfield** — deck import + binder import. Depends on buylist only for live card info overlay in phase 4. Can run parallel with 3-Buylist.

Both can run parallel since they touch different files entirely.

---

### Phase 4: Polish (4 agents, fully parallel after Phase 3)

- **4-Live card info** — Sort Session tab right panel. Touches `app.js`, `index.html` (Sort Session), `style.css`.
- **4-Session gallery/analytics** — new sub-area or separate modal, Chart.js integration. Touches `app.js` + `index.html` + new endpoints.
- **4-Calibration wizard** — modal content, uses existing calibration endpoints. Touches `app.js` + `index.html`.
- **4-Wishlist priority + notifications** — `web_worker.py` wishlist hook (careful — in scope fences), new endpoints, browser Notification API in JS.

Parallelization risk: all four touch `app.js`. Assign distinct sections and have each agent append its own IIFE / module. No cross-module JS calls.

---

## Rules for agents running in parallel

1. **Each agent works on its own git branch** forked from the last merged phase commit.
2. **No cross-branch imports** — if an agent needs something from another branch, stop and wait for merge.
3. **Pre-scaffolded stub functions** are the primary coordination mechanism (created in 0A).
4. **Write to separate files when possible.** When shared files are unavoidable (`app.js`, `web_server.py`), append rather than modify existing regions.
5. **Run the full test suite before requesting merge.** Probe tests included.
6. **Merge conflicts = stop and ask the user.** Don't auto-resolve; manual review required.

---

## Single-agent path (fallback)

If the user prefers one agent at a time, the sequential order is:

1. Phase 0A → 0B-1 → 0B-2 → 0B-3
2. Phase 1-Tagger → 1-EDHREC → 1-edhtop16 → 1-Spellbook
3. Phase 2-Filters → 2-Locator → 2-Cull
4. Phase 3-Buylist → 3-Moxfield
5. Phase 4-Live → 4-Gallery → 4-Wizard → 4-Wishlist

Total: 17 task handoffs. At ~1 hour each (optimistic), ~17 hours of focused work. Realistic with debugging: 30–50 hours.

Parallel path cuts wall-clock time roughly in half at the cost of more coordination overhead.

---

## Agent prompt template

For every task, the prompt to the implementation agent should include:

1. Link to this handoff directory and the specific feature section in `03_acceptance_criteria.md`.
2. The branch to create (`feature/phase1-tagger`, etc.).
3. Scope fence reminder: link `04_scope_fences.md`.
4. Shared interface link: `07_shared_interfaces.md`.
5. Acceptance criteria for the specific feature.
6. Instruction to run `tests/probes/run_all.py` and `pytest tests/` before declaring done.
7. Instruction to commit frequently with messages describing the change (no broad rewrites).

Example:
```
You are implementing Phase 1 Tagger cache. Read:
- plans/web_enrichment_plan.md
- plans/web_enrichment_source_probes.md (Tagger section)
- plans/handoff/04_scope_fences.md
- plans/handoff/07_shared_interfaces.md
- plans/handoff/03_acceptance_criteria.md (Tagger section)

Create branch feature/phase1-tagger from main.

Deliverables:
- probes/probe_tagger.py (or confirm existing probe passes)
- web_enrichment/tagger.py implementing EnrichmentSource interface
- tests/enrichment/test_tagger.py with fixtures in tests/fixtures/tagger/
- Register refresh endpoint in web_server.py (replace stub)

Acceptance: every bullet under "Scryfall Tagger cache" in 03_acceptance_criteria.md passes.

Do not modify anything listed in 04_scope_fences.md strictly-off-limits section.
```
