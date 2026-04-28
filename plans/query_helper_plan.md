# Query Helper Plan

## STATUS

Drafted 2026-04-27. Not started. **High priority** behind Bulk Edit
+ Right-Click Menu. Highest-ceiling feature on the roadmap — the otag
relationship map is reusable infrastructure that unlocks many other
features (auto-categorization, deck-hole finder, upgrade suggestions,
namespace-aware search, etc.) beyond the immediate query helper UX.

---

## Goal

Bridge "I have a specific card in mind" → "what queries describe this
card's role" → "which cards in MY collection fill that role". When zero
of the user's cards match, **relax the query** along well-defined axes
until matches appear, surfacing each relaxation as a labeled choice
rather than a black-box guess.

The feature is itself a thin UI layer. The hard part is the data:
building a typed graph of relationships between Scryfall Tagger's
oracle-tags, currently absent from the app. That graph powers the
relaxation, and once it exists, it powers a whole class of features
beyond this one.

---

## Two-layer architecture

```
┌─────────────────────────────────────────────────────────┐
│  Query Helper UI                                        │
│  (card picker → query candidates → apply → relaxation) │
└─────────────────────────────────────────────────────────┘
                          ↑
                          │ reads
                          │
┌─────────────────────────────────────────────────────────┐
│  Otag Knowledge Layer                                   │
│  - otags table (catalogue + namespace)                 │
│  - otag_relations table (8 typed relationships)         │
│  - scrape pipeline (tagger.scryfall.com)                │
│  - derivation pipeline (local card data)                │
└─────────────────────────────────────────────────────────┘
```

The knowledge layer ships first. The UI is small once the data exists.

---

## Phase 1 — Otag Knowledge Layer

### 1.1 Schema

```sql
CREATE TABLE otags (
    otag        TEXT PRIMARY KEY,
    description TEXT,                    -- short human description (from Tagger)
    namespace   TEXT,                    -- assigned in derivation: 'mechanical' | 'tribal' | 'flavor' | 'meta' | 'ability' | 'zone' | 'cost' | 'unknown'
    card_count  INTEGER NOT NULL DEFAULT 0,  -- cards in local Scryfall data with this otag
    last_scraped DATETIME,
    scrape_source TEXT                   -- 'tagger_scrape' | 'docs_page' | 'derived_only'
);

CREATE TABLE otag_relations (
    src_otag      TEXT NOT NULL,
    dst_otag      TEXT NOT NULL,
    relation_type TEXT NOT NULL,
        -- 'hierarchy'        — src is a child/specialization of dst
        -- 'synonym'          — src ≈ dst (≥0.95 Jaccard or marked by Tagger)
        -- 'sibling_disjoint' — src and dst share a parent but never co-occur
        -- 'co_occurs'        — src and dst share cards above a threshold
        -- 'implies'          — every card with src also has dst (logical implication)
        -- 'exclusion'        — implicit complement (e.g. 'land' / non-land cards)
        -- 'antagonistic'     — src answers / counters dst (e.g. removal/creature)
        -- 'related'          — soft "see also" for the visualization, weight low
    weight         REAL NOT NULL DEFAULT 1.0,  -- co-occurs uses 0..1 Jaccard; others 1.0
    source         TEXT NOT NULL,              -- 'tagger_scrape' | 'derived' | 'manual'
    last_updated   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (src_otag, dst_otag, relation_type)
);
CREATE INDEX idx_otag_relations_dst ON otag_relations(dst_otag, relation_type);
CREATE INDEX idx_otag_relations_type ON otag_relations(relation_type, weight DESC);
```

Storage cost: ~1500 otags × ~150 bytes = 225 KB. Relations are
denser — perhaps 50k–80k typed edges total = ~10 MB. Negligible.

Lives in `enrichment.db` (alongside `staples`, `salt_scores`, etc.) —
new `OtagSource` registered with the existing refresh scheduler.

### 1.2 Relationship type catalogue

Eight types. Each ships with a clear definition + source. The
relaxation engine treats each type differently.

#### A. Hierarchy
`src` is a child / specialization of `dst`. Multi-rooted DAG; one tag
can have multiple parents. `wrath-effect → creature-removal → removal`.
**Source**: scrape `tagger.scryfall.com/tags/<otag>` per-tag pages,
extract the parent links from the page header.

#### B. Synonym
`src ≈ dst`. Two paths to populate:
- **Marked**: when Tagger explicitly aliases two tags (rare, when present).
- **Derived**: Jaccard similarity ≥ 0.95 over the local card set.
**Source**: `tagger_scrape` for marked; `derived` for Jaccard.

#### C. Sibling-but-disjoint
`src` and `dst` share at least one parent in the hierarchy but their
card sets never overlap (or overlap < 1%). Powers a "don't suggest
siblings, climb to parent" rule in relaxation.
**Source**: `derived` from hierarchy + co-occurrence data.

#### D. Co-occurrence
`src` and `dst` appear on the same card more often than chance
predicts. Weight = lift score (P(both) / (P(src)·P(dst))) capped to
[0, 10]. The soft, edge-weighted layer of the graph.
**Source**: `derived` from local card data — pure SQL.

#### E. Implies (functional dependency)
Every card with `src` also has `dst`. Logical implication. Different
from hierarchy because the implication is mathematical, not categorical.
Example: `mana-rock → artifact → permanent`.
**Source**: `derived`.

#### F. Exclusion (implicit complement)
Pairs where having `src` implies NOT having `dst`. The cleanest cases
are by-construction (e.g. `land` and `nonland-card`) — these are
data-derivable. Soft exclusions (e.g. `creature` mostly excludes
`instant`) come from the data; threshold 0% co-occurrence.
**Source**: `derived` for clean cases; `manual` for soft cases.

#### G. Antagonistic / counterplay
`src` answers `dst`. `creature-removal` answers `creature`.
`counterspell` answers `instant-or-sorcery`. `graveyard-hate` answers
`graveyard-matters`. Not derivable from card data — purely semantic.
**Source**: `manual` curation. Probably 50–100 pairs total. Stored in
`card_data/manual_otag_antagonistic.json`.

#### H. Related (soft "see also")
Catch-all for tags that should appear near each other on the
visualization but don't fit any other relation. Example: `cantrip`
and `cheap-spell` aren't synonyms but they're tightly thematically
linked.
**Source**: `derived` from co-occurrence at low weight.

### 1.3 Namespace classification

After the hierarchy is scraped, run a clustering pass that assigns
each otag to one of 8 namespaces:

- **mechanical** — `ramp`, `removal`, `tutor`, `card-draw`
- **tribal** — `elf`, `dragon`, `vampire`, `phyrexian`
- **ability** — `flying`, `lifelink`, `vigilance`
- **flavor** — `pirate`, `samurai`, `villain`
- **zone** — `graveyard-matters`, `library-matters`, `exile-matters`
- **cost** — `cheap-spell`, `mana-rock`, `free-spell`
- **meta** — `commander-staple`, `cedh-staple`, `legacy-playable`
- **unknown** — fallback for tags that don't cluster cleanly

Heuristic: use the hierarchy roots + manual curation seed list, then
propagate via descent. This is a one-time pass that gets refreshed
quarterly with the scrape.

### 1.4 Scrape pipeline (`scrape_tagger.py`)

Two-stage:

**Stage 1: Catalogue.**
Fetch `https://scryfall.com/docs/tagger-tags` once. The page lists
every Tagger tag with a short description. Parse → upsert into the
`otags` table with `scrape_source='docs_page'`. Initial population.

**Stage 2: Per-tag relationships.**
For each otag in the catalogue, fetch
`https://tagger.scryfall.com/tags/oracle/<slug>`. Extract:
- Parent tag links (hierarchy edges)
- Aliased / synonym tags (synonym edges, when present)
- Description text (overrides docs-page description if richer)

Rate limit: 1 req/sec (matching `web_enrichment/edhrec.py`'s
`RATE_LIMIT_S = 1.0`). At ~1500 tags, total run is ~25 minutes.
Resumable via a `last_scraped` timestamp — partial runs are fine.

`scrape_tagger.py` lives in `web_enrichment/` and registers as an
`EnrichmentSource` with the existing scheduler. Defaults to
quarterly refresh — Tagger doesn't change quickly.

### 1.5 Derivation pipeline (`derive_otag_relations.py`)

Reads the local Scryfall bulk data (`default-cards-*.json`) +
the scraped hierarchy. Produces:

- **co_occurs** — every otag pair with ≥ 0.05 Jaccard (≥ 5% of
  cards with either tag have both). Estimated ~20-30k edges.
- **implies** — every otag pair where 100% of `src` cards also have
  `dst`. Estimated ~5k edges.
- **synonym** — Jaccard ≥ 0.95.
- **sibling_disjoint** — pairs with shared parent in hierarchy AND
  Jaccard < 0.01.
- **related** — Jaccard 0.01..0.05 (soft, low weight).

Plus the namespace classification pass.

Run after every scrape pass and after every Scryfall bulk-data
refresh. Pure SQL, runs in a minute or two.

### 1.6 Manual curation files

```
card_data/manual_otag_antagonistic.json     -- type G (counterplay)
card_data/manual_otag_namespace_seed.json   -- namespace bootstrap list
card_data/manual_otag_exclusions.json       -- type F soft exclusions
```

Small JSON files. Versioned. Curated by hand based on real domain
knowledge — maybe 200 lines total across the three.

### 1.7 Probe + tests

`probes/probe_tagger.py` — fetches one known tag (`removal`) and
asserts the scraper extracts ≥ 3 children. Fails the source's
`probe()` if scraping breaks.

`tests/enrichment/test_otag_relations.py`:
- Hierarchy edges parsed correctly from a fixed HTML fixture
- Derivation produces expected co-occurs / implies on a small synthetic
  card set
- Namespace classifier handles the seed list correctly
- Relaxation engine returns expected suggestions for a planted query

---

## Phase 2 — Query Helper UI

Builds on Phase 1 data.

### 2.1 Card picker

A search box at the top of a new `Query Helper` tab. Autocomplete
over `cards.CARDS_DATA` — same data the rest of the app uses.

When a card is picked, a panel shows:
- The card image (Scryfall normal-size)
- All otags on the card, grouped by namespace
- The card's color, type, CMC, current price

### 2.2 Query candidates

Below the picker, a list of auto-generated query candidates:

```
Sol Ring  [cmr #472]

Suggested queries:
  ─ Mechanical role ───────────────────────────────────
    [ Try ]  otag:mana-rock                         (1,247 cards)
    [ Try ]  otag:fast-mana                         (84 cards)
    [ Try ]  otag:ramp                              (3,021 cards)
  ─ Type ───────────────────────────────────────────────
    [ Try ]  t:artifact cmc<=1                      (412 cards)
  ─ Price tier ────────────────────────────────────────
    [ Try ]  usd>=1 otag:mana-rock                  (98 cards)
    [ Try ]  usd<1 otag:mana-rock                   (1,149 cards)
  ─ Composite ─────────────────────────────────────────
    [ Try ]  otag:mana-rock cmc<=2 c:colorless      (167 cards)
```

Each `[ Try ]` button apply-loads the query into the Collection page
(opens the Collection tab with the filter pre-filled).

Candidates are generated by a small per-card module that reads the
card's otags, filters out namespace='meta' (those make weak query
seeds), and constructs combinations.

### 2.3 Relaxation engine

When the user applies a query and gets 0 collection matches, a banner
appears with relaxation suggestions:

```
No matches in your collection for: usd>=10 otag:wrath-effect

Try one of these:
  1. Lower price tier:  usd>=5 otag:wrath-effect    (3 in collection)
  2. Generalize otag:   usd>=10 otag:removal        (12 in collection)
  3. Drop price filter: otag:wrath-effect           (8 in collection)
```

The engine searches a relaxation tree:

1. **Numeric relaxation** — drop `usd>=N` to next bucket (10→5→2→1→none),
   raise `cmc<=N` by one. Trivial.
2. **Hierarchy relaxation** — replace each `otag:X` with each parent
   (climbing to broader categories). Requires the hierarchy edges.
3. **Drop one filter** — try removing each filter clause individually.
4. **Sibling pivot** — replace `otag:X` with each sibling that shares
   a parent. Lower priority because the result set may be wildly
   different in nature; surface only when other relaxations also
   yielded zero.

Each relaxation candidate gets a count from the inventory query,
sorted by "narrowness preserved" (relaxations that change the query
the least, ranked first). Top 3-5 surfaced.

### 2.4 "Show in Scryfall" cross-link

Adjacent to each candidate query, a button: `[ View on Scryfall ]`.
Translates the DSL → Scryfall syntax via the same translator used
by the sort-config bin-query-preview button (see Cross-references).
Useful for "show me what cards EXIST that match this, not just what
I own".

---

## Phase 3 — 3D Otag Visualization

Optional but high-value once the data exists.

### 3.1 Tech

D3.js force-directed graph or Cytoscape.js. Both work in-browser with
no backend rendering. Three.js if we want true 3D — overkill for the
data shape; a 2D force layout with namespace-based clustering reads
well in 2D.

### 3.2 Visual encoding

| Visual | Maps to |
|---|---|
| Node size | `card_count` (log scale) |
| Node color | `namespace` |
| Edge color | `relation_type` (hierarchy=solid black, co_occurs=dashed blue, antagonistic=red, etc.) |
| Edge thickness | `weight` |
| Cluster | namespace-based force grouping |

### 3.3 Interactions

- Click any node → side panel showing the otag's defining cards
  (5 random samples with image preview, "show all" button)
- Shift-click two nodes → query their intersection: opens Collection
  with `otag:A otag:B` pre-filled
- Right-click any node → standard right-click menu (oh look, the
  menu pattern from `right_click_menu_plan.md` already works since
  nodes are card-context too)
- Search box pans/zooms to a named tag
- Filter sidebar: hide edges of certain types (e.g. "show only hierarchy
  + antagonism, hide co-occurrence" for cleaner view)

### 3.4 What it unlocks

Beyond browsing:
- Discovering tags the user didn't know about
- Finding deck synergy clusters ("oh, all my reanimator cards are in
  this corner")
- Spotting missing categories ("I have lots of `removal` but no
  `graveyard-hate` — that's an answer-suite hole")
- Auto-suggested deck themes from scanned cards' otag densities

---

## Phase 4 — Sort-Config Bin-Query Preview (small, separable)

Independent of Phase 1-3 but uses the same DSL → Scryfall translator
that Phase 2.4 needs.

In the sort-config builder UI, each bin's query input gets a 🔗 button.
Clicking translates the query and opens
`https://scryfall.com/search?q=<encoded>` in a new tab.

### 4.1 DSL → Scryfall translator

Direct map (work as-is on Scryfall):

| Our DSL | Scryfall |
|---|---|
| `usd>=1` / `usd<1` | `usd>=1` / `usd<1` |
| `t:creature` / `t:legend` | `t:creature` / `t:legend` |
| `c:rg` / `c:gw` | `c:rg` / `c:gw` |
| `otag:ramp` | `otag:ramp` |
| `r>=rare` | `r>=rare` |
| `cmc<=3` | `cmc<=3` |
| `name:exact "..."` | `!"..."` |

No mapping (drop with inline tooltip warning):

| Our DSL | Reason |
|---|---|
| `staple:any` / `staple:cedh` / `staple:archetype` | enrichment-only |
| `cull:true` | derived from oracle text + enrichment |
| `is:foilscan` / `is:detected_foil` | scan-time foil flag |
| `buylist:ck` / `buylist:ck>=1` | buylist enrichment |
| `name:in(...)` | could expand to `(!"a" or !"b" or ...)` but Scryfall has 1024-char query limit; punt |

Soft mapping (with tooltip flagging the approximation):

| Our DSL | Approximate Scryfall |
|---|---|
| `border:borderless` | `frame:showcase OR frame:extendedart` |

The translator lives in `web_enrichment/scryfall_query_translator.py`
and exports a single `to_scryfall(dsl) -> (scryfall_q, dropped_clauses)`
function. Reused by:
- Sort-config bin-query preview (Phase 4)
- Query Helper "View on Scryfall" button (Phase 2.4)
- Future "open this saved filter on Scryfall" actions

### 4.2 Tests

`tests/test_scryfall_query_translator.py` with one assertion per
mapping row above. Snapshot pinned so future DSL changes are
deliberate.

---

## Phasing & dependencies

```
        [Phase 4: DSL→Scryfall translator] (small, independent — ship first)
                    │
                    ▼
[Phase 1: Otag Knowledge Layer] ← BIG, biggest unknown
        │       │
        │       └─→ unlocks: Phase 2 (Query Helper UI)
        │       └─→ unlocks: Phase 3 (3D Otag Viz)
        │       └─→ unlocks: future auto-categorization, deck-hole finder, etc.
        │
        └─→ also reusable for sort-config bin queries
            (typing `otag:X` in a bin query could autocomplete from
             the same otags table)
```

### Phase 4 — v1 (target: 1-2 days)

Land before the others; provides immediate value via "view bin query
on Scryfall" and is dependency-free.

- [ ] `web_enrichment/scryfall_query_translator.py`
- [ ] 🔗 button on every sort-config bin query input
- [ ] Tooltip showing dropped clauses
- [ ] Test pinned mappings

### Phase 1 — v1 (target: 1-2 weeks of work + 25-min scrape run)

- [ ] Schema (`otags` + `otag_relations` tables in `enrichment.db`)
- [ ] `web_enrichment/scrape_tagger.py` — Stage 1 catalogue + Stage 2
      per-tag scrape, registered with refresh scheduler
- [ ] `web_enrichment/derive_otag_relations.py` — co_occurs, implies,
      synonym, sibling_disjoint, related derivation
- [ ] Namespace classification pass with seed list
- [ ] Manual curation files (antagonistic, exclusions)
- [ ] `probes/probe_tagger.py`
- [ ] Tests for parser + derivation
- [ ] First end-to-end run; spot-check 20 known otags for relationship
      correctness

### Phase 2 — v1 (target: 3-5 days, gated on Phase 1)

- [ ] `Query Helper` tab UI
- [ ] Card picker autocomplete
- [ ] Per-card query candidate generator
- [ ] Apply-to-Collection navigation
- [ ] Relaxation engine: numeric + hierarchy + drop-filter
- [ ] "View on Scryfall" cross-link (uses Phase 4)
- [ ] Tests for relaxation suggestions

### Phase 3 — v1 (target: 3-5 days, gated on Phase 1)

- [ ] D3 / Cytoscape integration
- [ ] Force-directed layout with namespace clustering
- [ ] Edge-type toggles in sidebar
- [ ] Click-to-detail panel
- [ ] Shift-click intersection action
- [ ] Right-click integration

---

## Open questions

1. **Tagger HTML stability**. The scraper depends on Tagger's page
   markup. If they redesign, the scraper breaks. Mitigation: store
   the scraped HTML alongside the parsed result so we can re-run
   parsing without re-fetching. Probe asserts a known shape.

2. **Sibling-disjoint computation cost**. O(N² × C) where N=otags,
   C=avg cards per tag. For N=1500, C=300: ~675M card lookups.
   Doable in a few minutes if we precompute a `card → otag set`
   index in memory. Plan for ~5 minutes runtime.

3. **What about cards.py's existing `otag` data?** Verify whether
   `CARDS_DATA` rows already carry oracle-tags from the Scryfall
   bulk dump. If yes, Phase 1 derivation reads them directly and
   we don't need Phase 1 Stage 2 scrape for the per-card index —
   only for the parent/child hierarchy. (The Scryfall bulk data
   _does_ ship card-level tags as far as I recall — confirm.)

4. **Manual antagonistic curation**: who maintains it? The 50-100
   pairs are stable enough that one curation pass plus quarterly
   tweaks should suffice.

5. **Query Helper relaxation: how aggressive?** Default to "first
   relaxation that yields ≥5 matches", with a "show me more
   relaxations" link. Tunable later from real usage.

---

## Cross-references

- Bulk Edit: `plans/bulk_edit_plan.md` — Query Helper apply-to-Collection
  uses the Bulk Edit checkbox + selection-bar infrastructure on the
  Collection page.
- Right-Click Menu: `plans/right_click_menu_plan.md` — every card row in
  Query Helper (picker, candidate result lists, viz nodes) uses the
  same context menu.
- Existing infra to reuse:
  - `web_enrichment/edhrec.py` (EnrichmentSource pattern, scheduler
    integration, rate limiting)
  - `enrichment.db` (already holds staples / salt / themes; otag
    relations fit naturally alongside)
  - `cards.py` `CARDS_DATA` (per-card otag list)
  - `query_parser.py` (DSL evaluator — Phase 4 builds on it)
