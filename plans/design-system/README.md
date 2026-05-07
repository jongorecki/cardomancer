# Cardomancer Design System

> A trading card sorting machine that scans cards and physically sorts them into bins. Mystical, whimsical, fantasy-coded — but a real piece of software-driven hardware.

Cardomancer is a desktop-scale Magic: The Gathering card-sorting robot. A vacuum-suction-cup head on a Marlin-controlled X/Z gantry picks a card from a source bin, places it on a staging platform, identifies it via a hybrid pHash + DINOv2 vision pipeline, and routes it to the right destination bin based on user-configured sort rules. The host software is Python (Flask + SocketIO + OpenCV + PyTorch) with a vanilla-JS web UI.

This design system covers two surfaces:

1. **The card sorter web UI** — the operator interface that runs on `localhost:5000`. Live camera feed, sort-preset configuration, bin layout, session controls.
2. **The marketing site** — outward-facing storytelling for the product. Hero, how-it-works, sort presets, gallery, pre-order.

The brand pulls a mystical/whimsical thread through both: arcane sigils, parchment-and-ink type, ritual language ("divine", "summon", "augur"), but always anchored to a clean, legible, technical core. **Function first; whimsy as accent**, not decoration.

---

## Sources

- **Codebase** — `Scripts/` (mounted via local-file access). Flask + SocketIO + Bootstrap 5 single-page app.
  - `Scripts/static/style.css` — current dark-theme stylesheet (929 lines)
  - `Scripts/static/tokens.css` — spacing, radius, type, shadow, motion tokens
  - `Scripts/templates/index.html` + `Scripts/templates/partials/*.html` — Jinja2 SPA
  - `Scripts/README.md`, `Scripts/PROJECT.md` — full system docs (host software + hardware)
- **Logo / mark** — `uploads/Screenshot 2026-05-07 133113.png` (copied to `assets/cardomancer-logo.png`)
- **Hardware spec** — see `Scripts/PROJECT.md`. Suction cup, X/Z gantry on 4080 extrusion, ASUS ROG Eye camera, ArUco-marker-driven bin calibration.

---

## Index

```
.
├── README.md                       This file
├── SKILL.md                        Cross-compatible Agent Skill manifest
├── colors_and_type.css             Color + typography CSS variables
├── fonts/                          Webfonts (Cormorant Garamond, Inter, JetBrains Mono)
├── assets/                         Logos, marks, sigils, brand imagery
├── preview/                        Design-system review cards (for the DS tab)
├── ui_kits/
│   ├── sorter/                     Operator UI — live sort interface, bin layout, presets
│   └── marketing/                  Product website — hero, how-it-works, pre-order
└── slides/                         (none — no decks were provided)
```

---

## CONTENT FUNDAMENTALS

The brand voice operates on two registers and **switches deliberately**:

### Register A — Technical Operator (sorter UI)
Concise, precise, no-nonsense. Like a workshop manual that respects you. Title-Case for headings, sentence case for body. Lower-case for inline tokens (`c:r`, `staple:cedh`).

- "Detect & Sort" — never "Click here to detect"
- "No active session" — not "Looks like there's no session running yet!"
- "Source bin" / "Destination bin" / "Staging platform" — proper noun-ified hardware terms
- Scryfall syntax is preserved verbatim: `cmc<=3`, `c:wr`, `t:creature`
- Errors are direct: "Hash DB out of date", "Camera frame upside down"
- No emoji in operator copy. The existing UI uses Unicode symbols (`☀ ⚙ 📷 ⟲`) sparingly for icon glyphs, never as decoration.

### Register B — Mystical Marketing
Earnest about the magical metaphor without being precious. The machine is a *cardomancer* — one who divines meaning from cards. Use ritual verbs and arcane nouns, but always paired with concrete technical truth.

- **Verbs** — divine, summon, augur, scry, conjure, sort, route, identify
- **Nouns** — sigil, ritual, divination, oracle, chamber, staging altar, the Sorting
- **Avoid** — "magic" as a generic adjective; cliché wizard-hat / D&D vocabulary
- Tone: confident, slightly tongue-in-cheek, never sarcastic

Examples (we vs you):
- ✅ "Drop a stack on the staging altar. The cardomancer does the rest."
- ✅ "Every scan logged. Every card located. Find any printing in seconds."
- ❌ "Welcome, brave wizard! Are you ready to embark on a magical sorting journey?"
- ❌ "Our AI-powered platform leverages advanced computer vision."

**Casing**: Headlines in Title Case for marketing, Sentence case for body. Operator UI uses Sentence case throughout except for proper-noun hardware terms.

**I vs you**: Marketing addresses *you* (the collector). Operator UI is impersonal — it describes the system, not the user. ("Source bin calibrated." not "You calibrated the source bin.")

**Numbers, units, code**: Always in monospace (`JetBrains Mono`). `~30k cards`, `CROP_SIZE = 745`, `pHash distance ≤ 82`.

---

## VISUAL FOUNDATIONS

### Colors

The palette is **deep indigo + parchment**, with a single warm-amber accent for ritual/sigil moments and the existing technical-status colors (green/yellow/red/cyan) preserved from the operator UI.

- **Indigo primary** `#383888` — wordmark, headlines, primary buttons, sigil ink
- **Indigo deep** `#1F1F4A` — backgrounds, navigation, modal scrim
- **Violet accent** `#534AB7` — secondary buttons, links, the tagline color
- **Parchment** `#F4EFE4` — light-mode background, marketing canvas
- **Ink** `#1B1A2E` — body text on parchment
- **Lavender mist** `#E8E8F8` — soft fills, hover states
- **Amber sigil** `#D4A537` — single-color accent for ritual moments (sigils, "divined" status, key marketing CTA glow)

The sorter operator UI keeps its dark-by-default mode (`#121418` page bg) and re-skins the existing `--accent-blue` etc. semantic tokens with the indigo/violet palette so every existing CSS selector picks up the new look without rewrites.

### Typography

- **Display serif** — `Cormorant Garamond` (700, 600). High-contrast humanist serif. The Cardomancer wordmark in the logo is hand-lettered but Cormorant's drawn-serif character with sharp terminals and a slightly squared bowl is the closest free-license match. **Substitution flagged** — see Caveats.
- **Body sans** — `Inter` (400, 500, 600). Workhorse for UI labels, body, navigation. Pragmatic, neutral, dense.
- **Mono** — `JetBrains Mono` (400, 500). Logs, code, query syntax, tokens, numbers. Slight humanist warmth keeps it from feeling clinical.
- **Tagline / micro** — `Inter` at very wide letter-spacing (0.18em). Mimics the logo's "TRADING CARD COLLECTION SYSTEM" treatment.

Hierarchy: marketing leans on the display serif for h1/h2; operator UI uses sans throughout, reserving the serif for product naming, modal titles, and the empty-state poetic copy.

### Spacing & layout

4px base scale, identical to the existing tokens.css: `--space-1` 4px → `--space-10` 64px. Most UI gutters are `--space-3` (12px) or `--space-4` (16px). Cards have `0.85rem` inner padding to match the existing dense workshop UI.

Marketing uses generous vertical rhythm — full-bleed sections separated by 96-128px of breathing room. The sorter UI is intentionally dense; the marketing site is intentionally airy.

### Backgrounds

- **Sorter UI** — Solid charcoal `#121418` panels on a slightly darker page surface. No gradients on content backgrounds, only on emphasized buttons (`#btn-detect` uses a top-bottom blue gradient — we'll keep that pattern but shift the gradient to indigo).
- **Marketing** — Parchment `#F4EFE4` primary canvas. **Subtle hand-drawn star/sigil patterns** as section dividers (SVG, low-opacity). Hero gets a single full-bleed indigo panel with a constellation of dots. No bluish-purple gradients (banned tropes); when we go dark on marketing it's the deep indigo `#1F1F4A`, flat.

### Animation

- Easing — `ease-out` everywhere. 120ms / 200ms / 320ms scale matching `--transition-fast/base/slow`.
- Sigil reveals on scroll (marketing) — 600ms gentle fade + 8px upward slide.
- Hover — opacity 0.85 OR 4% lighter background (no scale transforms).
- Press — translateY(1px), no shrink.
- The detect-button "ready" pulse and the e-stop glow are existing patterns we preserve verbatim.

### Hover & press states

- Buttons hover: 6% lighter background (filled) OR fill on outline buttons.
- Links hover: opacity 0.7, slight underline thickening.
- Press: translateY(1px), no color change (the user is already committed).
- Cards hover (marketing): 1px shadow lift + indigo border accent.

### Borders, shadows, elevation

- Border radius — `4px` for chips/badges, `8px` for cards, `12px` for hero panels, `999px` for pills.
- Shadows are the existing `--shadow-sm/md/lg` from `tokens.css`. We add one extra: **`--shadow-glow-sigil`** = `0 0 24px rgba(212, 165, 55, 0.25)` for amber-accented marketing CTAs.
- Marketing cards get a 1px parchment-darker border (`#E0D7C5`), no shadow, to feel printed.
- Operator cards keep the existing 1px `--border-card` divider system.

### Transparency & blur

Used sparingly. The priority-bin toast stack (`#priority-toast-stack`) keeps its existing semi-transparent look. Modals get an `rgba(31, 31, 74, 0.6)` indigo scrim instead of pure black. No backdrop-blur on the operator UI (perf-sensitive, runs alongside live camera streaming) — marketing can use it on sticky nav.

### Imagery

- Operator UI uses **only** live camera feeds, debug crops, and Scryfall card images at native resolution.
- Marketing has no real product photos yet (the machine is in design). Use **placeholder blocks** with the indigo `#383888` fill and a centered card-glyph SVG. **Flagged** — see Caveats. When real photos arrive, treatment is: warm-leaning, slight grain, indigo-tinted shadows, parchment-toned highlights.

### Corner treatment / "card" metaphor

The trading-card silhouette is the recurring shape: 2.5:3.5 aspect ratio, 12px radius, 1px border. We use card-shaped containers for marketing feature blocks and for the gallery. Sorter UI cards are rectangular — they're software panels, not cards.

### Iconography

See [ICONOGRAPHY](#iconography) below.

---

## ICONOGRAPHY

The codebase uses **HTML entity glyphs** as icons throughout (no icon font, no SVG sprite, no Lucide/Heroicons). Examples from the existing UI:

- `&#9788;` ☀ — theme toggle (sun)
- `&#9881;` ⚙ — settings
- `&#128247;` 📷 — camera health badge
- `&#8634;` ⟲ — reset / re-home
- `&#9654;` ▶ — continuous play
- `&#8630;` ↶ — undo
- `&#9776;` ☰ — expand/collapse all

This is intentional — a workshop tool that needs to render reliably without webfont loading. We preserve this convention in the operator UI.

For **marketing and brand assets**, we add a small set of **arcane-sigil SVGs** as the iconographic backbone. These are inspired by alchemical/astrological glyphs (the stylized dagger-and-arrow form in the Cardomancer logo's card glyph). They live in `assets/sigils/` and are used at 24px (inline UI), 48px (feature blocks), and 96px+ (hero accents).

The sigils we use:
- `sigil-card.svg` — the dagger/arrow card glyph from the logo (also the favicon)
- `sigil-eye.svg` — the all-seeing-eye for "scan/identify"
- `sigil-bin.svg` — a circle-with-rune for "destination/route"
- `sigil-stars.svg` — three-point star cluster for divine/random sort
- `sigil-spiral.svg` — spiral for "continuous mode"

The set is small on purpose. **No emoji** anywhere in operator or marketing copy. **No hand-drawn-feeling SVG icons elsewhere** — the sigils are the only place we draw, and they're styled as ink stamps, not illustrations.

For utility icons not covered by the sigil set (chevrons, x, check, plus), we continue to use Unicode entity glyphs.

---

## CAVEATS — please review

1. **Font substitutions** — I matched the Cardomancer wordmark to **Cormorant Garamond** (Google Fonts). The actual logo uses a hand-lettered or commercial serif (the "C" terminal and the spurred "r" suggest something like *Trajan Pro*, *Cinzel Decorative*, or a custom face). If you have the original font file, drop it in `fonts/` and I'll re-skin. If you want me to push harder on the match, I can also try Cinzel or Cormorant Upright.
2. **Tagline font** — the logo's tagline ("TRADING CARD COLLECTION SYSTEM") is a wide-tracked geometric sans. I'm using Inter at 0.18em letter-spacing, which is close but not identical. Real candidates: Montserrat, Avenir Next.
3. **No machine photos** — the hardware is still in design. All marketing imagery is placeholder. When you have CAD renders or photos, swap in.
4. **The amber accent (`#D4A537`)** is my addition — there's no warm color in the logo. I introduced it to give the brand a "ritual/sigil" moment. Easy to remove if you want to stay strictly indigo + parchment.
5. **Whimsy dial** — I tuned this to ~30% whimsy / 70% function. The marketing site leans into the mystical metaphor; the operator UI is almost untouched conceptually (palette refresh + serif modal titles). Tell me if you want more or less.
