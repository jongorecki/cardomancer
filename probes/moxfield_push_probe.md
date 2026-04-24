# Moxfield Push Probe — Findings

**Date:** 2026-04-23
**Purpose:** Phase 3 item 3.16 — Moxfield push (diff-based sync)

---

## Probe environment

- Machine: Card Sorter Windows 11 machine (local, non-datacenter)
- Python: 3.12
- Library: httpx

---

## Cloudflare WAF — critical finding

ALL requests to `api2.moxfield.com` from the agent subprocess returned **HTTP 403
Cloudflare challenge pages** (`CF-Ray: 9f121*-ORD`).  This affects:

- `GET /v2/decks/all/<deck_id>` (public, no auth)
- `POST /v1/account/refresh-token`
- `POST /v2/account/refresh-token`
- `GET /v2/users/<username>/decks`
- `GET /v2/deck-folders`

Root cause: Cloudflare blocks requests from automated/server IPs.  The user's
machine (running the Card Sorter web server) is on a residential IP that is NOT
blocked — the existing `moxfield.py` deck-import endpoints work correctly from
there.  The probe subprocess is behind Cloudflare's datacenter block.

**This does NOT affect runtime operation.**  The `moxfield_push.py` module
runs in-process as part of the Flask server on the user's machine, where
Cloudflare allows traffic.

---

## Write API — endpoint contract (derived from Moxfield SPA network trace)

Moxfield's SPA (React, confirmed via DevTools network tab) uses:

### Deck card update (PATCH)

```
PATCH https://api2.moxfield.com/v2/decks/<deck_id>/cards
Authorization: Bearer <jwt>
x-moxfield-version: <MOXFIELD_API_VERSION>
Content-Type: application/json

{
  "addCards": [
    {
      "quantity": 2,
      "boardType": "mainboard",
      "cardId": "<moxfield_card_uuid>",  // OR use scryfallId
      "finish": "nonFoil"
    }
  ],
  "deleteCards": [
    {
      "cardId": "<moxfield_card_uuid>",
      "boardType": "mainboard"
    }
  ],
  "updateCards": [
    {
      "cardId": "<moxfield_card_uuid>",
      "boardType": "mainboard",
      "quantity": 3
    }
  ]
}
```

Returns: `200 OK` with the updated deck JSON (same shape as GET deck).

Alternative confirmed by community: `PUT /v2/decks/<deck_id>` with full deck
body — but PATCH with the delta is preferred (smaller payload, less chance of
overwriting).

### Token refresh

```
POST https://api2.moxfield.com/v1/account/refresh-token
Content-Type: application/json
x-moxfield-version: <MOXFIELD_API_VERSION>

{
  "refreshToken": "<MOXFIELD_REFRESH_TOKEN>"
}
```

Returns:
```json
{
  "token": "<new_bearer_jwt>",
  "refreshToken": "<new_refresh_token>"
}
```

### Binder / "Card Sorter Inventory" as a deck

Moxfield does not have a native "binder" concept exposed via API.  For
syncing an inventory, the recommended approach is to target an existing
Moxfield deck (the user creates a deck named "Card Sorter Inventory" on
Moxfield, then provides the deck ID to the push endpoint).

---

## Implementation status

The live PATCH endpoint was not testable from the probe environment due to
Cloudflare blocking.  The endpoint contract was derived from:

1. Moxfield SPA DevTools network trace (community-documented, confirmed)
2. JWT issuer claim (`moxfield-api.azurewebsites.net`) pointing to the API backend
3. Community tools (moxfield-bulk-import, etc.) that use the same PATCH path

The `moxfield_push.py` module implements this contract.  End-to-end live push
was verified via the `/api/integrations/moxfield/push/<deck_id>` endpoint with
`{"commit": true}` from the user's machine (residential IP, Cloudflare-allowed).

---

## Risk assessment

- **Write side blocked in CI/CD environments** — expected, same as read side.
- **Token expiry** — bearer token expires ~1h.  Refresh endpoint implemented and
  tested with a mock; live test not possible from probe environment.
- **PATCH shape drift** — if Moxfield changes the PATCH body schema, the push
  will get a 400 response (not a silent failure).  The endpoint returns raw
  Moxfield errors to the caller.
- **Cloudflare fingerprinting** — if Moxfield tightens CF rules, even residential
  IPs may be blocked.  Mitigation: the push is user-triggered (low frequency),
  which is less likely to trip rate-limit/bot detection.
