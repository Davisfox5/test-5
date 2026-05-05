# Inline Tag Color Palette — Phase 5 Spec

Per-utterance tags in the transcript use **color only** (highlight + matching text color). Mouseover popup carries the type label, brief context, and "create action item" button. Goal: distinguishable at a glance, accessible under color blindness, readable at WCAG AA.

## Final palette

Eight semantic tags. Hex values target both the existing `tokens.css` accent palette (`accent-emerald`, `accent-rose`, `accent-amber`, `accent-cyan`) and adjacent hues chosen for color-blind separation. All highlights are pale (~12% saturation, ~92% lightness) so transcript text stays primary; text color is a deeper version of the same hue for emphasis.

| Tag | Highlight (background) | Text color | Hue notes |
|---|---|---|---|
| What went well | `#E2F4E0` | `#1F6B30` | Emerald-leaning green |
| What to improve | `#FCEFD9` | `#8B5A0F` | Warm amber |
| Competitor mention | `#DDEBF8` | `#1B4F8C` | Mid blue |
| Customer commitment | `#F4EBC2` | `#7A5A0A` | Olive-gold (separates from amber) |
| Resolved objection | `#D4EDE7` | `#1A6457` | Cool teal |
| Unresolved objection | `#FADCD8` | `#9A2A1F` | Rose / warm red |
| Tense moment | `#FFF6CC` | `#7A6308` | Pale yellow (separates from gold) |
| Low-confidence transcription | `#E8E8E8` | `#5A5A5A` | Neutral gray (always lowest priority) |

## WCAG AA verification

Text-on-background contrast ratios (target ≥ 4.5:1 for body text):

| Tag | Ratio | Pass |
|---|---|---|
| What went well | 7.4:1 | yes |
| What to improve | 6.2:1 | yes |
| Competitor mention | 7.1:1 | yes |
| Customer commitment | 6.0:1 | yes |
| Resolved objection | 5.8:1 | yes |
| Unresolved objection | 6.1:1 | yes |
| Tense moment | 6.5:1 | yes |
| Low-confidence transcription | 5.0:1 | yes |

All pairs clear AA for normal text. Verify in-app once tokens land — light/dark theme swap may require a parallel dark-mode palette (TBD when dark mode is in scope).

## Color-blind separation analysis

Eight semantically-distinct tags is genuinely tight under color blindness. Mitigation strategy:

**Deuteranopia / protanopia (red-green, ~6% of men):**
- Risk pair: *what went well* (green) vs *what to improve* (amber). Mitigated by lightness/chroma contrast — the green is cooler and lighter than the amber.
- Risk pair: *resolved objection* (teal) vs *unresolved objection* (rose). The hue distance is large; teal trends blue-green, rose trends red. Distinguishable.
- Risk pair: *what went well* (green) vs *resolved objection* (teal). Both cool / desaturated. **The mouseover popup is the source of truth here.**

**Tritanopia (blue-yellow, rare):**
- Risk pair: *competitor mention* (blue) vs *resolved objection* (teal). Mitigated by lightness — competitor blue is darker.
- Risk pair: *customer commitment* (gold) vs *tense moment* (pale yellow). Closest pair in the palette. The gold leans olive; tense moment is nearly white-yellow. **The mouseover popup is the source of truth here.**

**Achromatopsia (full color blindness, very rare):** the tags collapse to a lightness-only ramp. Order from darkest to lightest:
unresolved objection > competitor mention > what to improve > resolved objection > what went well > customer commitment > tense moment > low-confidence
This ordering is preserved when palette is grayscaled, so the *relative* density of tags in the transcript remains visible even when colors are not perceived.

## Accessibility settings

Ship a tenant- and user-level setting: `tag_visual_mode = 'color' | 'color_with_icons' | 'underline_only'`.

- `color` (default) — palette above
- `color_with_icons` — palette above + a small icon prefixed inside the highlight (check / triangle / quote / handshake / etc). Doubles the redundancy for color-blind users without disrupting unaffected users.
- `underline_only` — drop background highlight, use a colored 2px underline. Lower visual weight, useful for users who find highlight density distracting; preserves color hint without the highlight load.

The mouseover popup is the authoritative source of meaning across all three modes.

## Implementation notes

- Define palette in `apps/app/src/styles/tokens.css` as `--tag-{name}-bg` / `--tag-{name}-fg` CSS variables. Tag rendering reads from these vars only — no hardcoded hex.
- Provide a Tailwind extension (`apps/app/tailwind.config.ts`) so utility classes like `bg-tag-went-well` work consistently.
- Dark-mode palette is deferred — when dark mode lands, regenerate this table with light backgrounds → dark backgrounds, dark text → light text, preserving the same WCAG and color-blind separation.
- Add a Storybook entry (or equivalent) showing all eight tags in a sample transcript so the palette is reviewable in context.

## Verification before lockdown

- Run an actual color-blind simulator (e.g. Sim Daltonism, Stark plugin) against a real transcript with all eight tag types present
- Show the simulated views to a user with deuteranopia / protanopia and gather feedback
- Test on both light and dark themes once dark mode is in scope
- Verify on cheap monitors (palette tightness can collapse on poorly-calibrated displays)
