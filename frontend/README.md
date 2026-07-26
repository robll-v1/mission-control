# Frontend

React + TypeScript + Vite single-page console for PR review management.

![Review detail, light](../docs/screenshots/review-light.png)

<details>
<summary>Dark theme &amp; mobile</summary>

![Review detail, dark](../docs/screenshots/review-dark.png)
![Review detail, mobile](../docs/screenshots/review-mobile.png)

</details>

## Features
- Task list sidebar with status indicators; collapses to a drawer under 900px
- Verdict banner summarising the latest round: verdict, finding count, severity breakdown
- Findings list with severity rail, monospace `file:line`, and per-finding detail
- Review timeline with real-time SSE event streaming
- Review round history with per-round verdict, note, duration, and commit
- Artifacts tab for context snapshots, diffs, and summaries
- Modal-based task creation from GitHub PR URLs
- Light and dark themes; follows the OS preference, remembers an explicit choice

## Design system

Tokens live at the top of `src/styles.css`. Everything else consumes CSS custom
properties, so re-theming means editing one block.

| Group | Tokens |
|---|---|
| Surfaces | `--bg`, `--surface`, `--surface-sunken`, `--surface-hover` |
| Lines | `--border`, `--border-strong` |
| Text | `--text`, `--text-secondary`, `--text-muted` |
| Brand | `--accent`, `--accent-hover`, `--accent-soft`, `--accent-ring` |
| Semantic | `--ok`, `--warn`, `--danger`, `--neutral` (+ `-soft` fills) |
| Severity | `--sev-critical`, `--sev-high`, `--sev-medium`, `--sev-low` (+ `-soft`) |
| Type | `--font-sans`, `--font-serif`, `--font-mono` |

### Two moods

The themes are not the same design with the lights turned off.

- **Light — warm paper.** Surfaces carry a slight yellow cast. Hue does the
  semantic work: red / amber / slate / grey for the severity ramp.
- **Dark — matte monochrome.** No hue anywhere. Flat neutral surfaces, hairline
  borders, `--shadow` disabled so nothing reads as glossy. Meaning is carried by
  a **luminance ramp — brighter means more urgent** — so `--danger` is the
  brightest value in the theme and `--ok` one of the dimmest.

Because the dark ramp has no hue, severity must never depend on it alone. Every
state also carries a text label (`严重/高/中/低`, `审查失败/需要关注/未发现明显问题`),
and links carry a persistent underline rather than a colour shift.

Conventions worth keeping:

- **Colour means something.** Hue (light) or luminance (dark) is reserved for
  status, verdict, and severity. Chrome stays neutral in both.
- **Severity has its own ramp**, not aliases of the semantic colours — it needs
  four distinguishable steps, and every step is used as *text*, so all four must
  clear 4.5:1 on `--surface`.
- **Serif for voice, sans for UI.** The brand, page title, verdict word, and
  round number use `--font-serif`; controls and data use `--font-sans`.
- **Monospace for identifiers.** Paths, SHAs, and event kinds use `--font-mono`
  so they are scannable and never re-wrap mid-token.
- **Fonts are system stacks.** No webfont requests — this is a local-first tool
  and should render instantly offline.
- **Prose stays readable.** Long text is capped around 72–74 characters.
- Dark mode is driven by `data-theme` on `<html>`; `prefers-color-scheme` only
  seeds the initial value.
- `prefers-reduced-motion` disables the running-task pulse and transitions.

## Run
- From the repository root: `./scripts/dev-frontend.sh`
- Default: `http://127.0.0.1:5173` (proxies API to backend on `:8000`)

## Build
- From this directory: `npm run build`
- Type check only: `npx tsc --noEmit`
