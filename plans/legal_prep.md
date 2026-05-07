# Legal & ToS Landscape — Cardomancer

DRAFT — research synthesis dated 2026-05-07. NOT legal advice. Bring this
to a lawyer before making decisions.

Snapshot of public-facing terms and public news record. Where a source
returned 403 to direct fetch, I cite secondary reporting and flag the
gap in Open Questions. Quoted phrases are verbatim and short.

## Headline risks (the things to ask the lawyer first)

- **Wizards of the Coast Fan Content Policy is non-commercial by its
  literal text.** It says creators "can't sell or license your Fan
  Content to any third parties for any type of compensation"
  (company.wizards.com/en/legal/fancontentpolicy). A paid hardware
  product that ingests MTG card data, names, mana symbols, and set
  symbols is hard to fit inside that envelope without either a license
  or a careful argument that the product is not "Fan Content" at all.
- **Scryfall's data redistribution sits inside that same Fan Content
  framework.** Scryfall's own positioning is that it is "unofficial Fan
  Content permitted under the Wizards of the Coast Fan Content Policy"
  (per multiple cached references to scryfall.com/docs/terms). If
  Cardomancer ships Scryfall-derived bulk data inside a paid installer,
  the WoTC fan-content limit is the binding constraint, not Scryfall's
  own license.
- **Trademark exposure on the brand name.** I could not find any
  registered "Cardomancer" mark in USPTO/Justia surface search, but
  this is not a clearance opinion and the lawyer should run a real
  search across Nice classes 9 (software), 28 (games), and 41
  (entertainment).
- **Card Kingdom buylist scraping has no public license path.** No
  official buylist API was found in the research; the only sanctioned
  interaction is their CSV import tool. Scraping their pricing into a
  paid product is the highest legal-risk integration on the list.
- **Moxfield scraping is explicitly disfavored.** Moxfield has no
  public API, and community reports indicate scraping is treated as a
  ToS violation; the documented path is contacting support@moxfield.com
  for a custom User-Agent. A paid product that depends on undocumented
  endpoints is fragile both legally and operationally.

## Per-source summary

### Wizards of the Coast Fan Content Policy

Source: https://company.wizards.com/en/legal/fancontentpolicy

The policy is structured as numbered rules. Rule 1 (Free Content)
requires "free for everyone to access" content. Rule 4 (Don't Hurt
Wizards) prohibits "use Wizards' IP in other games" and prohibits
incorporating logos and trademarks into merchandise without written
consent. Rule 6 (Safe Sponsorship) permits only limited monetization
through ad revenue and sponsorship that doesn't paywall the audience.

The most directly load-bearing line for this product:
"You can't sell or license your Fan Content to any third parties"
(quoted from the Fan Content Policy page; phrasing confirmed in
multiple secondary references including
https://company.wizards.com/en/legal/fancontentpolicy and the EN World
analysis at
https://www.enworld.org/threads/the-other-snake-in-the-grass-the-wizards-fan-content-policy.694906/).

Ambiguity for the lawyer: the policy is written about creative content
(art, videos, podcasts, homebrew rules), not software interoperability.
A card sorter that recognizes a card by image hash is arguably closer to
a barcode scanner than to "fan content" in the policy's sense. The
policy also is not an exhaustive license — nominative use of trademarks
and fair use of imagery for indexing are governed by general IP law.

### Wizards trademark / IP enforcement track record

Pattern: WoTC has historically gone after products that (a) reproduce
card art at scale, (b) enable proxy creation, (c) attempt to create
parallel "formats" or markets. They have not, in the public record,
gone after physical inventory tools.

Documented actions:

- **Card Conjurer, Nov 2022.** Custom card creator received a C&D and
  shut down. Cited reason was reproduction of card text, art, and
  trademarks. Source:
  https://techraptor.net/tabletop/news/wizards-cds-card-conjurer-causing-closure
- **MTG Print, March 2023.** Proxy/print service received a C&D, went
  dark briefly, returned with the premade-proxy purchase option
  removed. Source:
  https://aetherhub.com/Article/WOTC-Sending-Cease--Desist-Letters-To-Proxy-Websites
- **mtgDAO NFT format, 2022.** C&D on a parallel NFT-based format
  using real cards. Source:
  https://www.pcgamer.com/unofficial-magic-the-gathering-nft-project-interrupted-as-wotc-summon-lawyers/

Not found in public record: C&Ds against card scanners (Delver Lens,
ManaBox), inventory tools (BinderPOS), deck builders (Moxfield,
Archidekt), or pricing tools (MTGStocks, EDHREC). These have coexisted
with Wizards for years behind prominent "not affiliated" disclaimers.

ManaBox's disclaimer, verbatim from
https://manabox.app/termsofservice/:
"ManaBox is an unofficial, fan made app and is NOT affiliated"
(rest continues with standard Wizards/Hasbro language).

### Scryfall ToS

Source: https://scryfall.com/docs/terms (returned 403 to direct fetch;
content reconstructed from cached references and from
https://curso-r.github.io/scryr/articles/imagery.html which mirrors
the official rules).

Confirmed verbatim phrases from the image-use rules
(https://curso-r.github.io/scryr/articles/imagery.html):

- "Card images on Scryfall are copyright Wizards of the Coast"
- "Do not cover, crop, or clip off the copyright or artist name"

This last one is directly relevant to Cardomancer's 745×1040 PNG
crops: the existing pipeline crops cards to the art region, which by
the literal rule cannot be the way images are *displayed* to users
without the copyright/artist visible nearby. Whether crops used purely
internally for hash matching (never displayed) fall under the rule is
a lawyer question.

Commercial use: Scryfall's general framing per cached references is
that data is "free of charge for the primary purpose of creating
additional Magic software" and that consumers of the data "may not
require anyone to make payments…in exchange for access to Magic
data." This is a paywall-on-the-data restriction, not necessarily a
paywall-on-your-product restriction; the distinction matters and
should be confirmed with a lawyer reading the live ToS.

Bulk data: Scryfall publishes daily bulk data files
(https://scryfall.com/docs/api/bulk-data). The research did not
surface an explicit prohibition on shipping a snapshot inside an
installer, but Scryfall's own framing inherits from the WoTC Fan
Content Policy, which is the binding upstream constraint.

Rate limits: Scryfall asks API consumers to throttle to ~10 requests
per second; this is documented at
https://scryfall.com/docs/api. Not load-bearing for a paid product
that relies on local bulk data, but load-bearing if the device ever
phones home for live lookups.

### Moxfield ToS

Source: https://www.moxfield.com (terms page returned 403 to direct
fetch; conclusions drawn from secondary sources and from public
community reporting).

What community sources consistently report:

- Moxfield does not publish an official API.
- Scraping is described as a ToS violation; the sanctioned path is
  emailing support@moxfield.com to request a custom User-Agent.
- Community reverse-engineered libraries (e.g.,
  https://github.com/Aleqsd/moxfield-api) exist and use Cloudflare
  bypass techniques, which Moxfield has shown willingness to block.

Cardomancer's current Moxfield integration is described as
"text paste-based, plus separate browser-session collection sync."
The text-paste path (user pastes their own decklist into Cardomancer)
is the lower-risk path. The "browser-session collection sync" path,
if it relies on automated requests against Moxfield endpoints, is the
exposure point.

### EDHREC ToS

Source: https://edhrec.com (terms page accessed; content paraphrased
in research, key clauses below).

Key clauses, paraphrased from the live document (research returned
section numbering but I am not reproducing long verbatim text):

- Section 2.2(a): license is for "personal, noncommercial use"; users
  may not commercially exploit the site.
- Section 2.2(b): prohibits derivative works and prohibits accessing
  the site to build a similar or competitive site.
- Section 3.1(iii) and (vi): prohibits harvesting/collecting data and
  prohibits automated agents/scripts for queries.

EDHREC has no documented official API. The research surfaced a
community library (`pyedhrec`) but no sanctioned commercial path.

For Cardomancer, the EDHREC integration is the most direct conflict
with the literal text of any ToS reviewed: a paid product calling an
automated scrape against a site whose terms forbid both commercial
exploitation and automated queries.

### Card Kingdom ToS

Source: https://www.cardkingdom.com/static/tos (returned 403 to
direct fetch; conclusions from secondary sources).

What is established from the public record:

- No public buylist API. Their sanctioned interaction is the CSV
  import tool at https://www.cardkingdom.com/static/csvImport.
- Community scraping tools exist (e.g.,
  https://github.com/dgoings/mtgscraper) but operate without a
  documented license.
- Card Kingdom is a commercial competitor in the trading-card retail
  space; scraping their pricing data into a competing paid product
  is the kind of use most retailers explicitly forbid.

The lawyer should be asked specifically: is there *any* sanctioned
path for a third-party paid product to display Card Kingdom buylist
prices? If not, this integration is a candidate for replacement
(TCGplayer has a documented commercial API, for example) or removal.

### Comparable paid products in the space

- **ManaBox** ($2.49/mo or $22.99/yr). Disclaimer:
  "ManaBox is an unofficial, fan made app and is NOT affiliated"
  (https://manabox.app/termsofservice/). Pattern: charge for a paid
  tier on top of an unofficial-fan-content disclaimer. Has not been
  publicly C&D'd.
- **Delver Lens.** Free with disclaimer that it is "not produced,
  endorsed, supported, or affiliated with Wizards of the Coast"
  (https://www.delverlab.com/, secondary references). Hardware-
  adjacent: phone-based card scanner.
- **BinderPOS.** Owned by TCGplayer; sells point-of-sale software for
  game stores (https://www.binderpos.com/). Stopped accepting new
  customers Feb 2025 per
  https://www.sleeveiq.com/tools/lgs/inventory-management. This is the
  closest precedent to a paid commercial MTG-data product, and it sits
  inside a TCGplayer commercial relationship, which is a different
  arrangement than Cardomancer would have.

The shared pattern across surviving paid products: prominent
"unofficial / not affiliated with Wizards" disclaimer; no use of WoTC
logos; product framed as helping a user manage their own collection
rather than as a creative work derived from MTG IP.

### Trademark — "Cardomancer"

Searches run: Justia Trademarks, Trademarkia (linked from search
results), USPTO TESS / new trademark search system. None returned a
"Cardomancer" mark in surface search. Sources consulted:

- https://tmsearch.uspto.gov/
- https://trademarks.justia.com/

This is **not** a clearance opinion. The lawyer should:

- Run a real search across at least classes 9 (software/electronic
  hardware), 28 (games and toys), and 41 (entertainment services).
- Search for phonetic/visual neighbors: "Cardomancer", "Cartomancer",
  "Cartomancy", "Card Omancer", "Cardmancer".
- Check WIPO Global Brand Database for international marks.
- Check common-law use (live websites, app stores, Etsy/Amazon stores)
  in addition to registered marks.

I noticed but did not analyze a "CARDMAKER" mark
(https://trademarks.justia.com/784/83/cardmaker-78483315.html) and
several Wizards-owned MTG marks
(https://trademarks.justia.com/owners/wizards-of-the-coast-inc-182235/).

## Mitigation options

For each headline risk, options to reduce exposure. These are
trade-offs to discuss with the lawyer, not recommendations.

**Risk: WoTC Fan Content Policy non-commercial language.**

- Option A: Reframe Cardomancer as a generic card-sorting appliance
  that happens to support MTG, with MTG support shipped as a
  user-installable plugin/data pack. Shifts the "Fan Content" question
  off the device manufacturer.
- Option B: Pursue a written license/acknowledgment from Wizards. Has
  precedent in licensed MTG accessories (Ultra Pro card sleeves,
  etc.). Slow, expensive, but conclusive.
- Option C: Strip MTG-specific branding entirely from the device,
  ship a generic appliance, and let third-party data providers handle
  the MTG layer.

**Risk: Scryfall data inside a paid installer.**

- Option A: Don't bundle. Have the device download Scryfall bulk data
  on first boot from the user's own internet connection, framed as
  the user's own access to a free public dataset.
- Option B: Bundle, but ship with a clear "powered by Scryfall data
  under the WoTC Fan Content Policy" attribution and link to source.
- Option C: Replace Scryfall with MTGJSON (https://mtgjson.com/) or a
  similar dataset whose terms are friendlier to bundling. Verify with
  the lawyer that MTGJSON's terms are actually different.

**Risk: Card Kingdom buylist scraping.**

- Option A: Drop Card Kingdom; switch to TCGplayer's commercial API.
- Option B: Have the user, not the device, fetch the buylist data.
- Option C: Drop pricing as a sort criterion until licensable.

**Risk: Moxfield integration.**

- Option A: Limit the integration to the user-paste decklist path,
  which doesn't touch Moxfield's servers from the device.
- Option B: Email support@moxfield.com and request the documented
  custom User-Agent path before shipping. Get the answer in writing.
- Option C: Drop Moxfield, integrate with Archidekt or another
  builder that has a documented API.

**Risk: EDHREC scraping.**

- Option A: Drop EDHREC, replace with EDHREC's own published
  staples lists (if any are licensable) or with the user's own data.
- Option B: Build the staples list manually from public sources (the
  user's own Moxfield exports, public tournament data, etc.) and ship
  it as a static dataset that the user can update.
- Option C: Email EDHREC, ask for a sanctioned path. Get the answer
  in writing.

**Risk: "Cardomancer" trademark conflict.**

- Option A: Run a full clearance with the lawyer before any public
  launch or domain purchase.
- Option B: Have a backup name ready (the project context already
  notes "Cardomancer is a play on cartomancy" — there's a family of
  variants the lawyer can grade).
- Option C: File an intent-to-use trademark application early in the
  product timeline, in the relevant Nice classes.

## Decisions the user needs to make

Concrete y/n questions for the lawyer:

1. Can Cardomancer ship as a paid product that locally uses MTG card
   names, mana symbols, and set symbols, given the WoTC Fan Content
   Policy's "can't sell or license your Fan Content" language?
2. Does the WoTC Fan Content Policy even apply to Cardomancer, or is
   the product better characterized as a non-fan-content product that
   makes nominative/functional use of MTG IP?
3. Can Cardomancer redistribute Scryfall bulk data inside its
   installer, or does it have to fetch on first boot?
4. Can Cardomancer display Scryfall-sourced art crops in the device
   UI, or must the full card image with copyright/artist always be
   shown?
5. Is "Cardomancer" clear to register in USPTO classes 9, 28, and
   41, after a real clearance search?
6. Do we drop Card Kingdom buylist integration, or is there a
   licensable path?
7. Do we replace the Moxfield browser-session collection sync with
   a sanctioned integration path, and if so which one?
8. Do we drop EDHREC scraping, given the literal "noncommercial use"
   and "no automated queries" language in their ToS?
9. What is the boilerplate disclaimer Cardomancer should ship with on
   the device, packaging, and website? (ManaBox/Delver Lens-style:
   "unofficial, not affiliated with Wizards of the Coast or Hasbro.")
10. Does the device need region-specific terms for EU/UK markets,
    where consumer protection and data rules differ from US?

## Open questions / things I couldn't find

- **Scryfall ToS direct verbatim text.** scryfall.com/docs/terms
  returned 403 to automated fetch. Cached references give the gist
  but the lawyer should pull the live document and read it cold.
- **Card Kingdom ToS direct verbatim text.** cardkingdom.com/static/tos
  returned 403. Same caveat.
- **Moxfield ToS direct verbatim text.** Same fetch issue. The
  community-reported behavior (scraping forbidden, custom User-Agent
  on request) is consistent across multiple unofficial libraries but
  is not a substitute for the live document.
- **Tagger / function-tag specific licensing.** Scryfall Tagger
  (otag:removal etc.) is a Scryfall-internal data product layered on
  top of WoTC IP. The research did not surface explicit licensing
  language for Tagger output as distinct from the rest of Scryfall.
- **Real Cardomancer trademark clearance.** Surface searches did not
  return a hit, but only a real attorney clearance covers common-law
  use, foreign marks, and phonetic/visual neighbors.
- **Recent (2025-2026) Wizards C&D activity.** The public record I
  could surface has the most recent specific actions in 2022-2023
  (Card Conjurer, MTG Print, mtgDAO). Absence of recent reporting is
  not evidence of inaction; it just means I couldn't surface it.
- **Hasbro/WoTC posture on hardware.** Every documented C&D in the
  research was against software/web tools or proxy-print services. No
  precedent specifically on a hardware device that interacts with MTG
  cards. The lawyer should not assume either way.
