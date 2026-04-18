# Data Source Probe Reference

Companion to `web_enrichment_plan.md`. Concrete guidance for how to call each external source, what data comes back, and what to verify.

**Confidence legend:**
- ✅ **Confirmed** — known endpoint, stable, response shape is well-documented.
- 🟡 **Partially known** — endpoint widely used but undocumented; fields are known empirically.
- 🔴 **Unknown** — need to probe before committing code. Starting points given but assume nothing.

Treat everything at 🟡 or below as "probe first, pin a snapshot, build against the snapshot."

---

## 1. Scryfall bulk data ✅

**How to get it:**
```
GET https://api.scryfall.com/bulk-data
```
Returns a catalog of bulk files. Download URLs for each file are in the response. The files we care about:
- `oracle_cards` — one entry per oracle (unique card) — ~30k entries
- `default_cards` — one entry per printing in default lang — ~500k entries (this is what you have in `default-cards-*.json`)
- `all_cards` — every printing in every language

**Request pattern:**
1. GET `/bulk-data` → find the object with `type == "oracle_cards"`
2. GET its `download_uri` → massive JSON array of card objects
3. Compare `updated_at` to last local copy; only re-download if newer

**Shape you already have** (confirmed from your local file):
```json
{
  "object": "card",
  "id": "<printing UUID>",
  "oracle_id": "<oracle UUID>",
  "name": "Forest",
  "lang": "en",
  "released_at": "2024-08-02",
  "layout": "normal",
  "image_uris": { "small": "...", "normal": "...", "art_crop": "..." },
  "mana_cost": "",
  "cmc": 0.0,
  "type_line": "Basic Land — Forest",
  "oracle_text": "...",
  "colors": [],
  "color_identity": ["G"],
  "keywords": [],
  "legalities": { "standard": "legal", ... },
  "prices": { "usd": "0.10", "usd_foil": "0.25", "eur": "0.08", "tix": "0.03" },
  "set": "blb",
  "collector_number": "280",
  ...
}
```

**Full field reference:** https://scryfall.com/docs/api/cards

**Fields we want for enrichment:**
- `oracle_id` (primary join key)
- `id` (printing key, for art tags)
- `name`, `oracle_text`, `type_line`, `mana_cost`, `cmc`, `colors`, `color_identity`, `keywords`
- `legalities.commander` for format filtering
- `prices.usd`, `prices.usd_foil` for price-tier sorting (refreshed daily)
- `set`, `collector_number` for Moxfield exact-printing matching

**Rate limit:** 50–100ms between requests. Bulk download is one big file, no rate-limit issue.

**Verification:**
- Card count in downloaded file should match the `size` field in the `/bulk-data` catalog entry.
- `updated_at` from catalog should be within last ~26 hours for daily refresh.

---

## 2. Scryfall search API ✅

**How to get it:**
```
GET https://api.scryfall.com/cards/search?q=<url-encoded query>&page=1
```

**Known-good examples:**
- `q=otag:removal` — all removal-tagged cards
- `q=otag:vanilla` — vanilla creatures
- `q=is:commander+legal:commander` — legendary creatures legal as commanders
- `q=usd>=5+legal:commander` — Commander-legal cards worth ≥$5

**Response shape:**
```json
{
  "object": "list",
  "total_cards": 1234,
  "has_more": true,
  "next_page": "https://api.scryfall.com/cards/search?q=...&page=2",
  "data": [ <card objects, same shape as bulk> ]
}
```

**Pagination:** follow `next_page` until `has_more` is false. 175 cards per page.

**`otag:` hierarchy behavior:** confirmed — querying a parent tag returns all descendants. So `otag:removal` returns spot-removal, sweepers, bounce, etc. This means leaf-only enumeration is sufficient; you don't need to flatten the tree client-side.

**Rate limit:** 50–100ms between requests. Enforce with a shared throttle.

**Error cases:**
- `404` on zero-result searches (not an error, empty result)
- `422` on malformed query syntax
- `429` on rate limit — back off 2s, retry

**Verification:**
- `total_cards` in first page response = expected count. Compare to Tagger's claimed count for this tag.

---

## 3. Scryfall Tagger GraphQL 🔴

**Endpoint (reverse-engineered, not official):**
```
POST https://tagger.scryfall.com/graphql
Content-Type: application/json
```

**Known empirically (from community tools):**
- Uses GraphQL POST with standard `{query, variables, operationName}` body shape.
- Likely requires session cookies or a CSRF token obtained from loading the site first — **needs probe to confirm**.
- Has queries for: tag list, tag detail (children/parents/card list), card-by-oracle-id (returns all tags on a card).

**What we don't know yet:**
- Exact operation names (`FetchTag`, `FetchAllTags`, etc. — community tools use different ones; names may have changed).
- Whether unauthenticated requests work, or if auth headers are required.
- Rate limit policy.
- Pagination shape on per-tag card lists.
- Full schema (no introspection query may be allowed).

**Probe steps (do these first, before writing cache code):**

1. **Network-tab reconnaissance.** Open tagger.scryfall.com in a browser with devtools. Navigate to a tag page (e.g., `/tags/card/removal`). Capture every GraphQL POST — headers, body, response. Save to `tests/probe_snapshots/tagger_tag_detail_raw.json`.

2. **Replay with curl / httpx.** Take the captured request, strip cookies one at a time, find the minimum auth required. If cookies are mandatory, build a session-login helper.

3. **Try introspection.** POST `{"query": "{ __schema { types { name } } }"}`. If it returns, schema is self-documenting and we can generate types. If blocked, rely on observed queries only.

4. **Pin a snapshot.** Once a working query is identified, save a known-good response as the shape-assertion fixture.

**If probe fails** (endpoint unreachable, auth impossible, schema changes frequently):
- **Fallback**: enumerate tags via the Scryfall search API instead. Maintain a manual tag list (community-maintained lists exist on GitHub; `scryfall-tagger-tags` or similar — verify currency before trusting). For each tag name, query `otag:<name>` on the search API. Works today without Tagger, just loses hierarchy/descriptions.

**Art tags (`atag:`):** same GraphQL endpoint, different type. Same probe procedure.

---

## 4. EDHREC JSON 🟡

**Endpoints (empirically known, undocumented):**

- **Commander page:** `https://json.edhrec.com/pages/commanders/<slug>.json`
  - Example: `https://json.edhrec.com/pages/commanders/atraxa-praetors-voice.json`
  - Slug = lowercase commander name, spaces → hyphens, apostrophes stripped
  
- **Top cards by timeframe:** `https://json.edhrec.com/pages/top/<period>.json`
  - `period` ∈ `{week, month, year, all}`
  - Returns top-cards lists, useful for global staples

- **Theme page:** `https://json.edhrec.com/pages/themes/<theme-slug>.json`
  - Example: `/pages/themes/lifegain.json`
  
- **Tribe page:** `https://json.edhrec.com/pages/tribes/<tribe-slug>.json`

- **Card page:** `https://json.edhrec.com/pages/cards/<card-slug>.json`
  - Returns per-card stats: inclusion, synergy, salt, prices, "also in" commanders

**Response shape (empirically):**
```json
{
  "container": {
    "json_dict": {
      "card_lists": [
        {
          "header": "Top Cards",
          "cardviews": [
            {
              "name": "Sol Ring",
              "sanitized": "sol-ring",
              "url": "/cards/sol-ring",
              "prices": { "tcgplayer": { "price": 1.23 }, ... },
              "inclusion": 0.95,           // % of decks running it
              "potential_decks": 50000,
              "num_decks": 47500,
              "synergy": 0.42,             // synergy score vs this commander
              "salt": 1.8,                 // salt score, only on some pages
              "label": "95% of 50000 decks"
            },
            ...
          ]
        },
        ...
      ]
    }
  }
}
```

**Warnings:**
- Field names have shifted over time. Schema-check each response; don't assume keys.
- Some pages have additional structures (combos section, tribal synergies, commander recommendations); only pull what you need.
- Not all pages have salt scores — salt is typically on the card page, not the commander page.

**Rate limit:** undocumented. Be gentle — 1 request per second max. Cache aggressively.

**Probe priorities:**
1. Pull one commander page, one theme page, one card page. Save each to fixtures.
2. Write a Pydantic model matching observed shape. Run against 20 more pages; treat any validation error as a schema-drift signal.
3. Check: does `inclusion` appear on every card in a commander's "Top Cards"? Is `synergy` always present? Log any nulls.

**Staple derivation (using this data):**
- **Universal staple**: card page shows `inclusion > 5%` across all decks, OR appears in `/pages/top/all.json` top 500.
- **Archetype staple**: card appears in top 100 of ≥ 3 theme pages.
- **Track `archetypes: [list]`** — write which theme pages a card appeared in. Enables theme-sort queries later.

---

## 5. edhtop16 GraphQL 🟡

**Endpoint:**
```
POST https://edhtop16.com/api/graphql
Content-Type: application/json
```

**Known:** their frontend is public and queries this endpoint; schema is intended for external use. Repo on GitHub has query examples (`lamperi/edhtop16` or similar — verify current location).

**Example query (schema needs verification via probe):**
```graphql
query TopDecks($first: Int!, $after: String, $timeframe: String) {
  tournaments(first: $first, after: $after, timePeriod: $timeframe) {
    edges {
      node {
        id
        name
        date
        topEntries {
          standing
          commander { name }
          decklist
        }
      }
      cursor
    }
    pageInfo { hasNextPage endCursor }
  }
}
```

**Warning:** the above field names are educated guesses based on typical Relay-style schemas — **probe first.** Expected workflow:
1. POST introspection query: `{"query": "{ __schema { queryType { fields { name } } } }"}` to see actual field names.
2. Once schema confirmed, write a real query for tournaments + decklists within a rolling window.
3. Paginate through all tournaments in last N months.

**Staple derivation:**
- Aggregate every decklist card count across the pulled window (e.g., 6 months).
- `inclusion_rate = times_included / total_decks_in_window`
- Threshold: `inclusion_rate > 0.15` for `staple:cedh` (cEDH is small; 15% of top-16 lists is significant).
- **Exclude basic lands** explicitly before staple calculation.

**Rate limit:** unknown; treat as 1 req/sec until proven otherwise.

---

## 6. Commander Spellbook ✅

**Endpoint:** `https://backend.commanderspellbook.com/` (REST)
Docs: https://commanderspellbook.com/api/ (verify current location)

**Relevant endpoints:**
- `GET /variants/` — paginated combos, each with `uses` (card list), `produces` (result), `identity`, `manaNeeded`, `prerequisiteList`
- `GET /variants/<id>/` — single combo detail

**Response shape (stable, documented):**
```json
{
  "count": 12345,
  "next": "...",
  "results": [
    {
      "id": "abc-123",
      "uses": [
        { "card": { "oracleCardId": "<oracle_id>", "name": "Thassa's Oracle" }, "quantity": 1 }
      ],
      "produces": [ { "feature": { "name": "Win the game" } } ],
      "identity": "UB",
      "manaNeeded": "{2}{U}",
      "prerequisiteList": [ "..." ]
    }
  ]
}
```

**Rate limit:** documented, modest. Paginate through everything, cache locally.

**Storage:** `combos(combo_id, result, identity, mana_needed)` + `combo_membership(oracle_id, combo_id)` for fast "is this card in a combo?" lookups.

**Verification:** `count` field matches expected total (currently in the tens of thousands).

---

## 7. Moxfield 🟡

**Endpoint:** `https://api2.moxfield.com/v2/` (unofficial but widely used)

**Known endpoints (reverse-engineered):**
- `GET /decks/all/<deck_id>` — full deck JSON
- `GET /users/<username>/decks` — user's public decks (paginated)
- Binder/collection endpoints exist for logged-in users; require bearer token from login flow

**Deck response shape (empirically):**
```json
{
  "id": "<deck_id>",
  "name": "Deck Name",
  "format": "commander",
  "publicUrl": "https://moxfield.com/decks/<id>",
  "mainboard": {
    "<card_key>": {
      "quantity": 1,
      "card": {
        "id": "<moxfield_card_id>",
        "scryfall_id": "<scryfall_printing_id>",
        "name": "Sol Ring",
        "set": "clb",
        "cn": "472",
        "oracle_id": "<oracle_id>"
      }
    },
    ...
  },
  "commanders": { ... },
  "sideboard": { ... }
}
```

**Fields we care about:**
- `mainboard.<key>.card.oracle_id` — for "any printing" matching
- `mainboard.<key>.card.set` + `cn` — for "exact printing" matching
- `mainboard.<key>.quantity`

**Authentication:**
- Public decks readable without auth.
- Collection / private binder access requires login — bearer token via `/v1/account/login` POST with email/password. Token goes in `Authorization: Bearer <token>` header.
- **Store credentials in env var, never commit.**

**Rate limit:** unknown; undocumented. Be conservative — 1 req/sec. Cache decks locally; don't re-fetch on every sort.

**Fragility warning:** endpoint paths have changed before (`v1` → `v2`). Build a probe that pins a known-stable deck and alerts if shape drifts.

**Verification:**
- `mainboard` card count matches what the public Moxfield UI shows.
- Every card has a populated `oracle_id`.

---

## 8. Buylists 🟡 (varies by vendor)

### CardKingdom
- Publishes buylist as a downloadable file: https://www.cardkingdom.com/purchasing/mtg_singles (verify current URL)
- Some community tools parse the HTML; there may be a JSON export. Probe first.
- **Simplest option:** scrape once a day, parse to `buylists(oracle_id, vendor='ck', price_usd)`.

### TCGPlayer
- Official API exists but requires partner approval (not easy to get).
- **Alternative:** Scryfall already stores `prices.usd` which is their market price, not buylist. No free buylist API.
- Community price sources: MTGGoldfish publishes vendor prices; can scrape.

### CardConduit
- Bulk submission service, not a traditional buylist.
- Their "bulk" pricing is tiered ($0.03/common, $0.25/rare, etc.) — can encode as static rules rather than an API call.
- They have a calculator on the site; if they expose JSON for it, use; else encode the published rates.

**For the dead-weight filter, you only need a boolean "has any buylist > threshold?" per card.** Even one vendor covers the use case. Start with CardKingdom only.

---

## 9. MTGJSON / AtomicCards

You already have `AtomicCards.json` locally. Good fallback for oracle data if Scryfall is down, but redundant with Scryfall bulk for our purposes. **Skip unless needed.**

---

## 10. strictlybetter.eu (deferred)

- Project repo: github.com/nbrehm/strictlybetter (verify current)
- Publishes a JSON dump of card pairs where one is strictly better than another.
- Download once, parse to a `dominated_by(worse_oracle_id, better_oracle_id)` table.
- Update infrequently (monthly at most).
- **Deferred until post-full-scan** per plan doc.

---

## Probe script template

Every source gets the same skeleton. Example for EDHREC:

```python
# probes/probe_edhrec.py
import json, httpx, hashlib, datetime
from pathlib import Path

SNAPSHOT_DIR = Path("tests/probe_snapshots")
PINNED = SNAPSHOT_DIR / "edhrec_atraxa_pinned.json"

def probe():
    url = "https://json.edhrec.com/pages/commanders/atraxa-praetors-voice.json"
    r = httpx.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    # Shape assertions — MUST match pinned schema
    assert "container" in data, "missing top-level 'container'"
    assert "json_dict" in data["container"]
    assert "card_lists" in data["container"]["json_dict"]
    lists = data["container"]["json_dict"]["card_lists"]
    assert any("cardviews" in L for L in lists), "no cardviews found"

    # Snapshot current response
    ts = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    out = SNAPSHOT_DIR / f"edhrec_atraxa_{ts}.json"
    out.write_text(json.dumps(data, indent=2))

    # Compare to pinned
    if PINNED.exists():
        pinned = json.loads(PINNED.read_text())
        diff = compare_keys(pinned, data)
        if diff:
            print(f"SCHEMA DRIFT: {diff}")
            return 1
    return 0

def compare_keys(a, b, path=""):
    """Recursively compare key sets. Returns list of differing paths."""
    diffs = []
    if isinstance(a, dict) and isinstance(b, dict):
        for k in set(a.keys()) ^ set(b.keys()):
            diffs.append(f"{path}.{k}")
        for k in set(a.keys()) & set(b.keys()):
            diffs.extend(compare_keys(a[k], b[k], f"{path}.{k}"))
    return diffs

if __name__ == "__main__":
    exit(probe())
```

Run all probes via `probes/run_all.py`. Pre-commit hook and pre-refresh hook both invoke this. Exit code ≠ 0 = do not refresh.

---

## What to probe first (recommended order)

1. **Scryfall bulk + search** (✅ confirmed) — write probe to pin card shape, catches Scryfall field changes.
2. **Commander Spellbook** (✅ confirmed) — easy win, validates pattern.
3. **EDHREC commander + card pages** (🟡) — critical for staples; pin shape early.
4. **edhtop16 GraphQL introspection** (🟡) — confirm schema before building query.
5. **Tagger GraphQL** (🔴) — hardest; budget time. Fallback ready if unreachable.
6. **Moxfield deck fetch** (🟡) — last because it's the most fragile and not on critical path for the core sorter.
7. **CardKingdom buylist scrape** (🟡) — only after dead-weight feature is being built.

Each probe: 30 min to an hour. Plan on a full day for the round of "first-pass probes + pin snapshots" before any enrichment code gets written.
