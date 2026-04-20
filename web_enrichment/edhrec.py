# web_enrichment/edhrec.py
# ---------------------------------------------------------------------------
# EDHRECSource: pulls staple / salt data from json.edhrec.com.
#
# Approach (2026-04-20):
#   - Archetype listing pages (themes/, tribes/, commanders/<slug>) return 403.
#     We maintain a curated ARCHETYPES dict (~80 archetypes, each with a list
#     of representative commander slugs).
#   - For each archetype we fetch up to MAX_COMMANDERS_PER_ARCHETYPE commander
#     pages; unique commanders are fetched once and counted toward every
#     archetype they represent.
#   - Universal staple:  inclusion_pct > UNIVERSAL_THRESHOLD (global, card page)
#   - Archetype staple:  card in top-100 of >= ARCHETYPE_MIN_COMMANDERS
#                        commander pages within a given archetype
#   - Salt:              directly from card page `salt` field
#
# Rate limit: 1 req/sec. Be gentle.
# ---------------------------------------------------------------------------

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

import httpx

import enrichment_db
from web_enrichment.base import EmitFn, EnrichmentSource, RefreshResult

logger = logging.getLogger(__name__)

RATE_LIMIT_S = 1.0
TIMEOUT_S = 30
USER_AGENT = "card-sorter-enrichment/0.1 (+https://example.invalid)"
BASE = "https://json.edhrec.com/pages"

UNIVERSAL_THRESHOLD = 0.05       # 5% global inclusion → universal staple
ARCHETYPE_MIN_COMMANDERS = 3     # must appear in ≥3 commander pages per archetype
MAX_COMMANDERS_PER_ARCHETYPE = 50
MAX_TOTAL_COMMANDERS = 700       # cap unique commander fetches per refresh

# ---------------------------------------------------------------------------
# Archetype catalogue.  Each key is an archetype name that will appear in
# archetypes_json on staple rows.  Values are commander slugs; we fetch up to
# MAX_COMMANDERS_PER_ARCHETYPE per archetype.  If a slug returns 403/error it
# is silently skipped.  Many commanders span multiple archetypes — they are
# fetched only once.
# ---------------------------------------------------------------------------
ARCHETYPES: dict[str, list[str]] = {

    # ── Strategy archetypes ─────────────────────────────────────────────────

    "artifacts": [
        "urza-lord-high-artificer",
        "breya-etherium-shaper",
        "sharuum-the-hegemon",
        "sai-master-thopterist",
        "emry-lurker-of-the-loch",
        "sydri-galvanic-genius",
        "oswald-fiddlebender",
        "glissa-the-traitor",
        "jhoira-weatherlight-captain",
        "brudiclad-telchor-engineer",
        "muzzio-visionary-architect",
        "feldon-of-the-third-path",
        "daretti-scrap-savant",
        "saheeli-the-gifted",
        "teshar-ancestors-apostle",
    ],

    "enchantress": [
        "sythis-harvests-hand",
        "zur-the-enchanter",
        "estrid-the-masked",
        "tuvasa-the-sunlit",
        "siona-captain-of-the-pyleas",
        "ardenn-intrepid-archaeologist",
        "ertai-the-corrupted",
        "lynde-cheerful-liquidator",
        "daxos-the-returned",
        "umbra-mystic",
    ],

    "spellslinger": [
        "niv-mizzet-parun",
        "niv-mizzet-the-firemind",
        "mizzix-of-the-izmagnus",
        "melek-izzet-paragon",
        "talrand-sky-summoner",
        "rielle-the-everwise",
        "zaffai-thunder-conductor",
        "veyran-voice-of-duality",
        "kalamax-the-stormsire",
        "Lier-disciple-of-the-drowned",
        "baral-chief-of-compliance",
        "shu-yun-the-silent-tempest",
    ],

    "graveyard-value": [
        "meren-of-clan-nel-toth",
        "muldrotha-the-gravetide",
        "karador-ghost-chieftain",
        "sidisi-brood-tyrant",
        "the-gitrog-monster",
        "tasigur-the-golden-fang",
        "gisa-and-geralf",
        "araumi-of-the-dead-tide",
        "jarad-golgari-lich-lord",
        "chainer-dementia-master",
        "nethroi-apex-of-death",
        "syr-konrad-the-grim",
        "wilhelt-the-rotcleaver",
        "lier-disciple-of-the-drowned",
        "volo-guide-to-monsters",
    ],

    "reanimator": [
        "kaalia-of-the-vast",
        "sedris-the-traitor-king",
        "geth-lord-of-the-vault",
        "razaketh-the-foulblooded",
        "vilis-broker-of-blood",
        "whisper-blood-liturgist",
        "feldon-of-the-third-path",
        "syr-konrad-the-grim",
        "chainer-dementia-master",
        "ink-treader-nephilim",
    ],

    "aristocrats": [
        "teysa-karlov",
        "prossh-skyraider-of-kher",
        "yawgmoth-thran-physician",
        "ghave-guru-of-spores",
        "judith-the-scourge-diva",
        "korvold-fae-cursed-king",
        "mazirek-kraul-death-priest",
        "savra-queen-of-the-golgari",
        "kels-fight-fixer",
        "meren-of-clan-nel-toth",
        "jarad-golgari-lich-lord",
        "lyzolda-the-blood-witch",
        "edgar-markov",
        "zulaport-cutthroat",
    ],

    "tokens": [
        "rhys-the-redeemed",
        "adrix-and-nev-twincasters",
        "lathril-blade-of-the-elves",
        "krenko-mob-boss",
        "purphoros-god-of-the-forge",
        "hazezon-tamar",
        "rith-the-awakener",
        "teysa-orzhov-scion",
        "alela-artful-provocateur",
        "darien-king-of-kjeldor",
        "ghave-guru-of-spores",
        "prossh-skyraider-of-kher",
        "marrow-gnawer",
        "chatterfang-squirrel-general",
        "lyssandra-storm-witch",
    ],

    "lifegain": [
        "lathiel-the-bounteous-dawn",
        "karlov-of-the-ghost-council",
        "ayli-eternal-pilgrim",
        "oloro-ageless-ascetic",
        "heliod-sun-crowned",
        "willowdusk-essence-seer",
        "vito-thorn-of-the-dusk-rose",
        "trostani-selesnyas-voice",
        "dina-soul-steeper",
        "arvad-the-cursed",
        "sanguine-bond",
        "garna-the-bloodflame",
    ],

    "stax": [
        "derevi-empyrial-tactician",
        "brago-king-eternal",
        "hokori-dust-drinker",
        "lavinia-azorius-renegade",
        "thalia-guardian-of-thraben",
        "grand-arbiter-augustin-iv",
        "urza-lord-high-artificer",
        "zur-the-enchanter",
        "hushbringer",
        "augustin-iv",
    ],

    "combo": [
        "thrasios-triton-hero",
        "tymna-the-weaver",
        "yawgmoth-thran-physician",
        "kess-dissident-mage",
        "najeela-the-blade-blossom",
        "niv-mizzet-parun",
        "zur-the-enchanter",
        "opus-thief",
        "kenrith-the-returned-king",
        "breya-etherium-shaper",
        "gitrog-monster",
        "heliod-sun-crowned",
        "selvala-heart-of-the-wilds",
        "yisan-the-wanderer-bard",
    ],

    "infect": [
        "atraxa-praetors-voice",
        "skithiryx-the-blight-dragon",
        "ezuri-claw-of-progress",
        "hapatra-vizier-of-poisons",
        "saskia-the-unyielding",
        "sidar-kondo-of-jamuraa",
        "yeva-natures-herald",
    ],

    "counters": [
        "atraxa-praetors-voice",
        "ezuri-claw-of-progress",
        "ghave-guru-of-spores",
        "hapatra-vizier-of-poisons",
        "vorel-of-the-hull-clade",
        "ozolith-the-shattered-spire",
        "willowdusk-essence-seer",
        "nikara-lair-scavenger",
        "hallar-the-firefletcher",
        "branching-evolution",
    ],

    "voltron": [
        "sram-senior-edificer",
        "ardenn-intrepid-archaeologist",
        "wyleth-soul-of-steel",
        "galea-kindler-of-hope",
        "kemba-kha-regent",
        "sigarda-host-of-herons",
        "zur-the-enchanter",
        "uril-the-miststalker",
        "skullbriar-the-walking-grave",
        "bruna-light-of-alabaster",
    ],

    "landfall": [
        "omnath-locus-of-rage",
        "omnath-locus-of-creation",
        "aesi-tyrant-of-gyre-strait",
        "mina-and-denn-wildborn",
        "tatyova-benthic-druid",
        "muldrotha-the-gravetide",
        "the-gitrog-monster",
        "ob-nixilis-the-fallen",
        "nissa-vastwood-seer",
        "rampaging-baloths",
    ],

    "big-mana": [
        "selvala-heart-of-the-wilds",
        "omnath-locus-of-mana",
        "azusa-lost-but-seeking",
        "titania-protector-of-argoth",
        "ulvenwald-hydra",
        "rishkar-peema-renegade",
        "marwyn-the-nurturer",
        "karametra-god-of-harvests",
        "gargos-vicious-watcher",
    ],

    "blink": [
        "brago-king-eternal",
        "roon-of-the-hidden-realm",
        "yorion-sky-nomad",
        "aminatou-the-fateshifter",
        "conjurer-s-closet",
        "venser-the-sojourner",
        "soulherder",
        "ephara-god-of-the-polis",
        "chulane-teller-of-tales",
    ],

    "theft": [
        "marchesa-the-black-rose",
        "yasova-dragonclaw",
        "xanathos-scheming-syndicate",
        "dack-fayden",
        "rubinia-soulsinger",
        "lazav-dimir-mastermind",
        "merieke-ri-berit",
        "thada-adel-acquisitor",
    ],

    "mill": [
        "phenax-god-of-deception",
        "lazav-dimir-mastermind",
        "gyruda-doom-of-depths",
        "traumatize",
        "bruvac-the-grandiloquent",
        "mirko-vosk-mind-drinker",
        "oona-queen-of-the-fae",
        "araumi-of-the-dead-tide",
    ],

    "clone": [
        "adrix-and-nev-twincasters",
        "brudiclad-telchor-engineer",
        "riku-of-two-reflections",
        "lazav-dimir-mastermind",
        "sakashima-of-a-thousand-faces",
        "sakashima-the-impostor",
        "zndrsplt-eye-of-wisdom",
        "okaun-eye-of-chaos",
    ],

    "wheel": [
        "niv-mizzet-parun",
        "niv-mizzet-the-firemind",
        "nekusar-the-mindrazer",
        "kynaios-and-tiro-of-meletis",
        "the-locust-god",
        "ob-nixilis-of-the-black-oath",
        "windfall",
        "jace-wielder-of-mysteries",
    ],

    "pillowfort": [
        "oloro-ageless-ascetic",
        "zedruu-the-greathearted",
        "ghostly-prison",
        "gahiji-honored-one",
        "pramikon-sky-rampart",
        "norn-s-annex",
        "kazuul-tyrant-of-the-cliffs",
    ],

    "extra-turns": [
        "narset-enlightened-master",
        "jhoira-of-the-ghitu",
        "teferi-temporal-archmage",
        "the-time-monster",
        "kalamax-the-stormsire",
        "yennett-cryptic-sovereign",
    ],

    "burn": [
        "purphoros-god-of-the-forge",
        "torbran-thane-of-red-fell",
        "krenko-mob-boss",
        "neheb-the-eternal",
        "firesong-and-sunspeaker",
        "kalamax-the-stormsire",
        "jaya-s-immolating-inferno",
    ],

    "group-hug": [
        "kynaios-and-tiro-of-meletis",
        "zedruu-the-greathearted",
        "phelddagrif",
        "gilanra-caller-of-wirewood",
        "kenrith-the-returned-king",
        "breena-the-demagogue",
    ],

    "chaos": [
        "yidris-maelstrom-wielder",
        "chaos-warp",
        "jhoira-of-the-ghitu",
        "possibility-storm",
        "hive-mind",
        "zndrsplt-eye-of-wisdom",
        "okaun-eye-of-chaos",
        "goblin-game",
    ],

    "planeswalkers": [
        "atraxa-praetors-voice",
        "aminatou-the-fateshifter",
        "teferi-temporal-archmage",
        "carth-the-lion",
        "kenrith-the-returned-king",
        "superfriends",
        "nissa-who-shakes-the-world",
    ],

    # ── Tribal archetypes ───────────────────────────────────────────────────

    "elves": [
        "lathril-blade-of-the-elves",
        "ezuri-renegade-leader",
        "rhys-the-redeemed",
        "selvala-heart-of-the-wilds",
        "marwyn-the-nurturer",
        "freyalise-llanowar-s-fury",
        "joraga-warcaller",
        "skullmulcher",
        "galadriel-of-lothorien",
        "alwen-exarch-of-gilder",
    ],

    "goblins": [
        "krenko-mob-boss",
        "purphoros-god-of-the-forge",
        "grenzo-dungeon-warden",
        "ib-halfheart-goblin-tactician",
        "wort-the-raidmother",
        "muxus-goblin-grandee",
        "skirk-prospector",
    ],

    "zombies": [
        "gisa-and-geralf",
        "wilhelt-the-rotcleaver",
        "the-scarab-god",
        "sidisi-brood-tyrant",
        "thraximundar",
        "grimgrin-corpse-born",
        "varina-lich-queen",
        "liliana-heretical-healer",
    ],

    "vampires": [
        "edgar-markov",
        "drana-liberator-of-malakir",
        "olivia-voldaren",
        "anje-falkenrath",
        "crimson-vow-commander",
        "licia-sanguine-tribune",
        "markov-patriarch",
    ],

    "humans": [
        "thalia-guardian-of-thraben",
        "odric-lunarch-marshal",
        "king-darien-xlviii",
        "saffi-eriksdotter",
        "jirina-kudro",
        "ravos-soultender",
        "silvar-devourer-of-the-free",
        "maja-bretagard-protector",
    ],

    "dragons": [
        "the-ur-dragon",
        "dragonstorm",
        "sarkhan-vol",
        "kaalia-of-the-vast",
        "miirym-sentinel-wyrm",
        "draconic-domination",
        "atarka-world-render",
        "bladewing-the-risen",
        "ojutai-soul-of-winter",
    ],

    "angels": [
        "avacyn-angel-of-hope",
        "kaalia-of-the-vast",
        "lyra-dawnbringer",
        "linvala-keeper-of-silence",
        "akroma-vision-of-ixidor",
        "gisela-blade-of-goldnight",
        "bruna-light-of-alabaster",
    ],

    "spirits": [
        "azorius-spirits",
        "millicent-restless-revenant",
        "reyhan-last-of-the-abzan",
        "kamigawa-spirits",
        "drogskol-reaver",
        "oyobi-who-split-the-heavens",
    ],

    "merfolk": [
        "kumena-tyrant-of-orazca",
        "sygg-river-guide",
        "lord-of-atlantis",
        "thada-adel-acquisitor",
        "merrow-reejerey",
        "vodalian-illusionist",
    ],

    "soldiers": [
        "darien-king-of-kjeldor",
        "odric-lunarch-marshal",
        "captain-sisay",
        "thalia-guardian-of-thraben",
        "general-kudro-of-drannith",
        "harbin-vanguard-aviator",
    ],

    "wizards": [
        "niv-mizzet-parun",
        "inalla-archmage-ritualist",
        "baral-chief-of-compliance",
        "azami-lady-of-scrolls",
        "adeliz-the-cinder-wind",
        "jalira-master-polymorphist",
        "jace-vryn-s-prodigy",
    ],

    "slivers": [
        "the-first-sliver",
        "sliver-overlord",
        "sliver-queen",
        "sliver-hivelord",
        "sliver-legion",
    ],

    "cats": [
        "kemba-kha-regent",
        "arahbo-roar-of-the-world",
        "mirri-weatherlight-duelist",
        "brimaz-king-of-oreskos",
        "grimalkin",
    ],

    "wolves": [
        "arlinn-kord",
        "tovolar-dire-overlord",
        "ulrich-of-the-krallenhorde",
        "wolf-of-devil-s-breach",
    ],

    "elementals": [
        "omnath-locus-of-rage",
        "omnath-locus-of-creation",
        "risen-reef",
        "horde-of-notions",
        "lord-of-shatterskull-pass",
    ],

    "dinosaurs": [
        "gishath-suns-avatar",
        "zacama-primal-calamity",
        "etali-primal-storm",
        "atla-palani-nest-tender",
        "marauding-raptor",
    ],

    "pirates": [
        "malcolm-keen-eyed-navigator",
        "breeches-brazen-plunderer",
        "ramirez-depietro-corsair",
        "dockside-extortionist",
        "beckett-brass",
    ],

    "knights": [
        "syr-gwyn-hero-of-ashvale",
        "edgar-markov",
        "sidar-kondo-of-jamuraa",
        "aryel-knight-of-windgrace",
        "haakon-stromgald-scourge",
    ],

    "ninjas": [
        "yuriko-the-tigers-shadow",
        "satoru-umezawa",
        "rograhk-champion-of-orazca",
    ],

    "rogues": [
        "yuriko-the-tigers-shadow",
        "anowon-the-ruin-thief",
        "oona-queen-of-the-fae",
        "sygg-river-cutthroat",
        "zareth-san-the-trickster",
    ],

    "snakes": [
        "kaseto-orochi-archmage",
        "ishkanah-grafwidow",
        "seshiro-the-anointed",
    ],

    "clerics": [
        "orah-skyclave-hierophant",
        "karlov-of-the-ghost-council",
        "heliod-sun-crowned",
        "daxos-the-returned",
    ],

    # ── Color-pair themes ───────────────────────────────────────────────────

    "azorius-control": [
        "raff-capashen-ships-mage",
        "isperia-the-inscrutable",
        "lavinia-azorius-renegade",
        "brago-king-eternal",
        "ephara-god-of-the-polis",
        "grand-arbiter-augustin-iv",
        "hanna-ships-navigator",
        "dragonlord-ojutai",
    ],

    "dimir-control": [
        "yuriko-the-tigers-shadow",
        "phenax-god-of-deception",
        "lazav-dimir-mastermind",
        "oona-queen-of-the-fae",
        "araumi-of-the-dead-tide",
        "mirko-vosk-mind-drinker",
        "anowon-the-ruin-thief",
    ],

    "rakdos-sacrifice": [
        "judith-the-scourge-diva",
        "mogis-god-of-slaughter",
        "lyzolda-the-blood-witch",
        "xantcha-sleeper-agent",
        "anje-falkenrath",
        "rakdos-lord-of-riots",
        "olivia-voldaren",
    ],

    "gruul-aggro": [
        "xenagos-god-of-revels",
        "ruric-thar-the-unbowed",
        "wort-the-raidmother",
        "klothys-god-of-destiny",
        "radha-heart-of-keld",
        "atarka-world-render",
    ],

    "selesnya-tokens": [
        "rhys-the-redeemed",
        "trostani-selesnyas-voice",
        "emmara-soul-of-the-accord",
        "selvala-explorer-returned",
        "karametra-god-of-harvests",
        "sigarda-host-of-herons",
    ],

    "orzhov-lifegain": [
        "karlov-of-the-ghost-council",
        "ayli-eternal-pilgrim",
        "teysa-karlov",
        "kambal-consul-of-allocation",
        "licia-sanguine-tribune",
        "elenda-the-dusk-rose",
    ],

    "izzet-spells": [
        "niv-mizzet-parun",
        "mizzix-of-the-izmagnus",
        "jhoira-weatherlight-captain",
        "kalamax-the-stormsire",
        "veyran-voice-of-duality",
        "zaffai-thunder-conductor",
        "melek-izzet-paragon",
    ],

    "simic-counters": [
        "atraxa-praetors-voice",
        "vorel-of-the-hull-clade",
        "ezuri-claw-of-progress",
        "tatyova-benthic-druid",
        "zegana-utopian-speaker",
        "chulane-teller-of-tales",
        "aesi-tyrant-of-gyre-strait",
    ],

    "boros-equipment": [
        "wyleth-soul-of-steel",
        "sram-senior-edificer",
        "ardenn-intrepid-archaeologist",
        "kemba-kha-regent",
        "gisela-blade-of-goldnight",
        "aurelia-the-warleader",
        "depala-pilot-exemplar",
    ],

    "golgari-graveyard": [
        "meren-of-clan-nel-toth",
        "muldrotha-the-gravetide",
        "jarad-golgari-lich-lord",
        "glissa-the-traitor",
        "mazirek-kraul-death-priest",
        "savra-queen-of-the-golgari",
        "the-gitrog-monster",
        "tayam-luminous-enigma",
    ],

    # ── Shard / Wedge themes ────────────────────────────────────────────────

    "esper-control": [
        "zur-the-enchanter",
        "oloro-ageless-ascetic",
        "sharuum-the-hegemon",
        "sydri-galvanic-genius",
        "breya-etherium-shaper",
        "aminatou-the-fateshifter",
    ],

    "grixis-control": [
        "nekusar-the-mindrazer",
        "kess-dissident-mage",
        "sedris-the-traitor-king",
        "marchesa-the-black-rose",
        "thraximundar",
        "yidris-maelstrom-wielder",
    ],

    "jund-midrange": [
        "prossh-skyraider-of-kher",
        "korvold-fae-cursed-king",
        "karrthus-tyrant-of-jund",
        "xira-arien",
        "sek-kuar-deathkeeper",
    ],

    "naya-tokens": [
        "gishath-suns-avatar",
        "zacama-primal-calamity",
        "mayael-the-anima",
        "rith-the-awakener",
        "hazezon-tamar",
        "atla-palani-nest-tender",
    ],

    "bant-value": [
        "chulane-teller-of-tales",
        "roon-of-the-hidden-realm",
        "derevi-empyrial-tactician",
        "tuvasa-the-sunlit",
        "amareth-the-lustrous",
    ],

    "mardu-aggro": [
        "kaalia-of-the-vast",
        "edgar-markov",
        "alesha-who-smiles-at-death",
        "zurgo-helmsmasher",
        "mathas-fiend-seeker",
    ],

    "temur-spells": [
        "animar-soul-of-elements",
        "riku-of-two-reflections",
        "omnath-locus-of-rage",
        "kalamax-the-stormsire",
        "ur-dragon",
    ],

    "sultai-value": [
        "muldrotha-the-gravetide",
        "tasigur-the-golden-fang",
        "sidisi-brood-tyrant",
        "anowon-the-ruin-thief",
        "thrasios-triton-hero",
        "tymna-the-weaver",
    ],

    "jeskai-control": [
        "narset-enlightened-master",
        "shu-yun-the-silent-tempest",
        "ishai-ojutai-dragonspeaker",
        "raugrin-triome",
        "vadrok-apex-of-thunder",
    ],

    "abzan-midrange": [
        "nethroi-apex-of-death",
        "ghave-guru-of-spores",
        "karador-ghost-chieftain",
        "anafenza-the-foremost",
        "tayam-luminous-enigma",
        "teneb-the-harvester",
    ],

    # ── 4-colour / 5-colour ─────────────────────────────────────────────────

    "5-color-goodstuff": [
        "kenrith-the-returned-king",
        "najeela-the-blade-blossom",
        "the-ur-dragon",
        "sliver-overlord",
        "golos-tireless-pilgrim",
        "jodah-archmage-eternal",
        "sisay-weatherlight-captain",
    ],

    "4-color-sans-white": [
        "yidris-maelstrom-wielder",
        "atraxa-praetors-voice",
        "breya-etherium-shaper",
        "thrasios-triton-hero",
        "tymna-the-weaver",
    ],

    "4-color-sans-green": [
        "breya-etherium-shaper",
        "atraxa-praetors-voice",
        "saskia-the-unyielding",
        "yidris-maelstrom-wielder",
    ],
}


def _build_commander_archetype_map() -> dict[str, list[str]]:
    """Invert ARCHETYPES → {commander_slug: [archetype, …]}."""
    out: dict[str, list[str]] = {}
    for archetype, slugs in ARCHETYPES.items():
        for s in slugs[:MAX_COMMANDERS_PER_ARCHETYPE]:
            out.setdefault(s, []).append(archetype)
    return out


def _slug(name: str) -> str:
    """Convert a card name to an EDHREC URL slug."""
    s = name.lower()
    s = re.sub(r"[',]", "", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _extract_cardlists(data: dict) -> list[dict]:
    return (data.get("container", {})
               .get("json_dict", {})
               .get("cardlists", []))


class EDHRECSource(EnrichmentSource):
    """Pull universal/archetype staples + salt scores from EDHREC."""

    name = "edhrec"

    def __init__(self):
        self._client: Optional[httpx.Client] = None

    def probe(self) -> bool:
        from probes.probe_edhrec import probe as _probe
        r = _probe()
        return r.ok

    def refresh(self, emit: Optional[EmitFn] = None,
                full: bool = False) -> RefreshResult:
        start = time.time()
        errors: list[str] = []
        warnings: list[str] = []

        if not self.probe():
            msg = "EDHREC probe failed; aborting."
            errors.append(msg)
            _record(self.name, False, msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)

        name_map = _build_name_map()

        # Build {commander_slug: [archetypes]} from ARCHETYPES catalogue
        commander_to_archetypes = _build_commander_archetype_map()
        # Unique commander slugs, capped to MAX_TOTAL_COMMANDERS
        all_slugs = list(commander_to_archetypes.keys())[:MAX_TOTAL_COMMANDERS]

        with httpx.Client(
            timeout=TIMEOUT_S,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
        ) as client:
            self._client = client

            self._emit(emit, 0, 5, "Fetching top/year global cards …")
            top_cards = self._fetch_top_year(warnings)

            self._emit(emit, 1, 5,
                       f"Fetching {len(all_slugs)} commander pages …")
            commander_data = self._fetch_commanders(all_slugs, emit, warnings)

            unique_card_slugs = set(
                cv.get("sanitized") or _slug(cv["name"])
                for lst in commander_data.values()
                for cv in lst[:100]
                if cv.get("name")
            )
            for cv in top_cards:
                if cv.get("sanitized"):
                    unique_card_slugs.add(cv["sanitized"])

            self._emit(emit, 2, 5,
                       f"Fetching {len(unique_card_slugs)} card pages for salt …")
            card_details = self._fetch_card_pages(
                list(unique_card_slugs), emit, warnings)

            self._client = None

        self._emit(emit, 3, 5, "Computing staple tiers …")
        staple_rows, salt_rows, theme_rows = self._compute_enrichment(
            top_cards, commander_data, card_details, name_map, warnings,
            commander_to_archetypes=commander_to_archetypes,
        )

        self._emit(emit, 4, 5, "Writing enrichment to DB …")
        rows_changed = 0
        coverage_pct = 0.0
        conn = enrichment_db.get_connection()
        try:
            rows_changed = self._write(conn, staple_rows, salt_rows,
                                       theme_rows, warnings)
            total_staples = conn.execute(
                "SELECT COUNT(*) FROM staples WHERE source='edhrec'"
            ).fetchone()[0]
            coverage_pct = float(total_staples) / max(1, len(staple_rows)) * 100
            enrichment_db.record_sync_attempt(
                conn, self.name, success=True, coverage_pct=coverage_pct,
                version_hash=str(total_staples))
            enrichment_db.record_coverage(
                conn, self.name, key_name="universal_staples",
                expected=len([r for r in staple_rows if r["tier"] == "universal"]),
                actual=total_staples)
        except Exception as exc:
            msg = f"DB write error: {type(exc).__name__}: {exc}"
            logger.exception("EDHREC DB write error")
            errors.append(msg)
            enrichment_db.record_sync_attempt(conn, self.name, False, error=msg)
            return RefreshResult(source=self.name, success=False,
                                 duration_ms=_ms(start), errors=errors)
        finally:
            conn.close()

        self._emit(emit, 5, 5,
                   f"Done. {rows_changed} rows, {len(salt_rows)} salts.")
        return RefreshResult(
            source=self.name, success=True,
            duration_ms=_ms(start),
            rows_changed=rows_changed,
            coverage_pct=coverage_pct,
            warnings=warnings,
        )

    def coverage_report(self) -> dict:
        conn = enrichment_db.get_connection()
        try:
            sync = conn.execute(
                "SELECT * FROM sync_metadata WHERE source = ?",
                (self.name,)
            ).fetchone()
            stats = conn.execute(
                """SELECT tier, COUNT(*) as cnt FROM staples
                   WHERE source='edhrec' GROUP BY tier"""
            ).fetchall()
            archetype_names = conn.execute(
                """SELECT DISTINCT archetypes_json FROM staples
                   WHERE source='edhrec' AND tier='archetype'
                   AND archetypes_json IS NOT NULL"""
            ).fetchall()
            unique_archetypes: set[str] = set()
            for row in archetype_names:
                try:
                    unique_archetypes.update(json.loads(row[0]))
                except Exception:
                    pass
            return {
                "source": self.name,
                "staples_by_tier": {r["tier"]: r["cnt"] for r in stats},
                "archetype_count": len(unique_archetypes),
                "salt_count": conn.execute(
                    "SELECT COUNT(*) FROM salt_scores").fetchone()[0],
                **(dict(sync) if sync else {}),
            }
        finally:
            conn.close()

    # -- Fetch helpers -------------------------------------------------------

    def _get(self, url: str) -> Optional[dict]:
        try:
            time.sleep(RATE_LIMIT_S)
            r = self._client.get(url)
            if r.status_code == 403:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.debug("EDHREC GET %s failed: %s", url, exc)
            return None

    def _fetch_top_year(self, warnings: list[str]) -> list[dict]:
        data = self._get(f"{BASE}/top/year.json")
        if not data:
            warnings.append("top/year.json returned no data (rate-limited?)")
            return []
        cardlists = _extract_cardlists(data)
        if not cardlists:
            warnings.append("top/year.json: no cardlists found")
            return []
        return cardlists[0].get("cardviews", [])

    def _fetch_commanders(
        self,
        slugs: list[str],
        emit: Optional[EmitFn],
        warnings: list[str],
    ) -> dict[str, list[dict]]:
        """Fetch commander pages for all unique slugs; map slug → top-100 cards."""
        out: dict[str, list[dict]] = {}
        total = len(slugs)
        for i, slug in enumerate(slugs):
            if i % 20 == 0:
                self._emit(emit, 1, 5,
                           f"Commander pages: {i}/{total} done …")
            data = self._get(f"{BASE}/commanders/{slug}.json")
            if not data:
                warnings.append(f"Commander page unavailable: {slug}")
                continue
            cardlists = _extract_cardlists(data)
            cards: list[dict] = []
            for lst in cardlists:
                cards.extend(lst.get("cardviews", []))
            out[slug] = cards[:100]
        return out

    def _fetch_card_pages(
        self,
        slugs: list[str],
        emit: Optional[EmitFn],
        warnings: list[str],
    ) -> dict[str, dict]:
        out: dict[str, dict] = {}
        total = len(slugs)
        for i, slug in enumerate(slugs):
            if i % 50 == 0:
                self._emit(emit, 2, 5, f"Card pages: {i}/{total} fetched …")
            data = self._get(f"{BASE}/cards/{slug}.json")
            if not data:
                continue
            jd = data.get("container", {}).get("json_dict", {})
            card = jd.get("card")
            if isinstance(card, dict) and card.get("name"):
                out[slug] = card
        return out

    # -- Computation ---------------------------------------------------------

    def _compute_enrichment(
        self,
        top_cards: list[dict],
        commander_data: dict[str, list[dict]],
        card_details: dict[str, dict],
        name_map: dict[str, str],
        warnings: list[str],
        commander_to_archetypes: Optional[dict[str, list[str]]] = None,
    ) -> tuple[list[dict], list[dict], list[dict]]:
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        if commander_to_archetypes is not None:
            # Per-archetype counting: {oracle_id: {archetype: commander_count}}
            archetype_hits: dict[str, dict[str, int]] = {}
            for slug, cards in commander_data.items():
                archetypes = commander_to_archetypes.get(slug, [])
                for cv in cards[:100]:
                    name = cv.get("name")
                    if not name:
                        continue
                    oid = _lookup(name, name_map)
                    if not oid:
                        continue
                    card_hits = archetype_hits.setdefault(oid, {})
                    for arch in archetypes:
                        card_hits[arch] = card_hits.get(arch, 0) + 1
        else:
            # Flat commander count (backwards compat / test mode)
            flat_count: dict[str, list[str]] = {}
            for slug, cards in commander_data.items():
                for cv in cards[:100]:
                    name = cv.get("name")
                    if not name:
                        continue
                    oid = _lookup(name, name_map)
                    if not oid:
                        continue
                    if slug not in flat_count.get(oid, []):
                        flat_count.setdefault(oid, []).append(slug)

        staple_rows: list[dict] = []
        salt_rows: list[dict] = []
        theme_rows: list[dict] = []
        seen_universal: set[str] = set()

        # Universal staples from card pages
        for slug, card in card_details.items():
            name = card.get("name") or (card.get("names") or [None])[0]
            if not name:
                continue
            oid = _lookup(name, name_map)
            if not oid:
                continue

            num_decks = card.get("num_decks") or 0
            potential = card.get("potential_decks") or 0
            salt = card.get("salt")

            if potential > 0 and (num_decks / potential) >= UNIVERSAL_THRESHOLD:
                if oid not in seen_universal:
                    seen_universal.add(oid)
                    staple_rows.append({
                        "oracle_id": oid,
                        "tier": "universal",
                        "source": "edhrec",
                        "score": num_decks / potential,
                        "archetypes_json": None,
                        "last_updated": ts,
                    })

            if salt is not None:
                salt_rows.append({
                    "oracle_id": oid,
                    "salt": float(salt),
                    "last_updated": ts,
                })

        # Universal from top/year (backup for cards without card pages)
        for cv in top_cards:
            name = cv.get("name")
            if not name:
                continue
            oid = _lookup(name, name_map)
            if not oid or oid in seen_universal:
                continue
            num_decks = cv.get("num_decks") or 0
            potential = cv.get("potential_decks") or 0
            if potential > 0 and (num_decks / potential) >= UNIVERSAL_THRESHOLD:
                seen_universal.add(oid)
                staple_rows.append({
                    "oracle_id": oid,
                    "tier": "universal",
                    "source": "edhrec",
                    "score": num_decks / potential,
                    "archetypes_json": None,
                    "last_updated": ts,
                })

        # Archetype staples
        if commander_to_archetypes is not None:
            for oid, arch_counts in archetype_hits.items():
                qualifying = [
                    arch for arch, cnt in arch_counts.items()
                    if cnt >= ARCHETYPE_MIN_COMMANDERS
                ]
                if qualifying:
                    best_score = max(arch_counts[a] for a in qualifying)
                    total_commanders = len(commander_data)
                    staple_rows.append({
                        "oracle_id": oid,
                        "tier": "archetype",
                        "source": "edhrec",
                        "score": best_score / max(1, total_commanders),
                        "archetypes_json": json.dumps(sorted(qualifying)),
                        "last_updated": ts,
                    })
        else:
            # Flat mode
            for oid, cmdr_slugs in flat_count.items():
                if len(cmdr_slugs) >= ARCHETYPE_MIN_COMMANDERS:
                    staple_rows.append({
                        "oracle_id": oid,
                        "tier": "archetype",
                        "source": "edhrec",
                        "score": len(cmdr_slugs) / max(1, len(commander_data)),
                        "archetypes_json": json.dumps(cmdr_slugs),
                        "last_updated": ts,
                    })

        return staple_rows, salt_rows, theme_rows

    # -- DB write ------------------------------------------------------------

    @staticmethod
    def _write(
        conn,
        staple_rows: list[dict],
        salt_rows: list[dict],
        theme_rows: list[dict],
        warnings: list[str],
    ) -> int:
        changed = 0
        with conn:
            conn.executemany(
                """INSERT INTO staples
                       (oracle_id, tier, source, score,
                        archetypes_json, last_updated)
                   VALUES (:oracle_id, :tier, :source, :score,
                           :archetypes_json, :last_updated)
                   ON CONFLICT(oracle_id, tier, source) DO UPDATE SET
                       score = excluded.score,
                       archetypes_json = excluded.archetypes_json,
                       last_updated = excluded.last_updated""",
                staple_rows,
            )
            changed += len(staple_rows)

            conn.executemany(
                """INSERT INTO salt_scores (oracle_id, salt, last_updated)
                   VALUES (:oracle_id, :salt, :last_updated)
                   ON CONFLICT(oracle_id) DO UPDATE SET
                       salt = excluded.salt,
                       last_updated = excluded.last_updated""",
                salt_rows,
            )
            changed += len(salt_rows)
        return changed

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _emit(emit: Optional[EmitFn], step: int, total: int,
              message: str) -> None:
        if emit is None:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            emit("enrichment_refresh_progress", {
                "source": "edhrec",
                "step": step, "total": total,
                "message": message, "ts": ts,
            })
        except Exception:
            pass


def _build_name_map() -> dict[str, str]:
    """Load name → oracle_id from card_universe."""
    conn = enrichment_db.get_connection()
    try:
        rows = conn.execute(
            "SELECT name, oracle_id FROM card_universe").fetchall()
        return {r["name"]: r["oracle_id"] for r in rows}
    finally:
        conn.close()


def _lookup(name: str, name_map: dict[str, str]) -> Optional[str]:
    return name_map.get(name)


def _record(source: str, success: bool, error: Optional[str] = None) -> None:
    conn = enrichment_db.get_connection()
    try:
        enrichment_db.record_sync_attempt(conn, source, success, error=error)
    finally:
        conn.close()


def _ms(start: float) -> int:
    return int((time.time() - start) * 1000)
