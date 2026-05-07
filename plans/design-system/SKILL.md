---
name: cardomancer-design
description: Use this skill to generate well-branded interfaces and assets for Cardomancer (a trading card sorting machine that scans and physically sorts cards), either for production or throwaway prototypes/mocks. Contains essential design guidelines, colors, type, fonts, assets, and UI kit components for prototyping.
user-invocable: true
---

Read the README.md file within this skill, and explore the other available files.

If creating visual artifacts (slides, mocks, throwaway prototypes, etc), copy assets out and create static HTML files for the user to view. If working on production code, you can copy assets and read the rules here to become an expert in designing with this brand.

If the user invokes this skill without any other guidance, ask them what they want to build or design, ask some questions, and act as an expert designer who outputs HTML artifacts _or_ production code, depending on the need.

## Quick orientation

- **Brand axis**: deep indigo (`#383888`) + parchment (`#F4EFE4`), with a single warm-amber accent (`#D4A537`) reserved for ritual/sigil moments.
- **Type**: Cormorant Garamond (display serif), Inter (sans), JetBrains Mono (code).
- **Tone**: function-first technical clarity in the operator UI; mystical-but-grounded ritual language in marketing. Never "magic-as-adjective".
- **Two surfaces**: `ui_kits/sorter/` (dark workshop UI) and `ui_kits/marketing/` (parchment one-pager).
- **Iconography**: HTML entity glyphs in operator UI; small set of arcane SVG sigils in `assets/sigils/` for marketing.

Load `colors_and_type.css` from any HTML you build — it provides the full token system and semantic type helpers (`.h1`, `.eyebrow`, `.mono`, etc).
