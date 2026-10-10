# Dockt design language — for Lane 1

Oct 10, 2026 · Brandon Shen (Lane 2)

## Why this exists

The tray, quick-chat popup and launcher should look like a small window of the dashboard, not a separate app. Everything below is taken from `server/static/index.html` on branch `lane2`. If the two ever disagree, the code wins and this doc needs an update.

- **Warm and quiet.** Cream paper in light mode, warm charcoal in dark mode. Thin borders, almost no shadow, no gradients.
- **Green is the brand, used sparingly.** NVIDIA green (`#76B900`) appears on one primary action, citation chips, focus rings and the logo dot. Never as a big background.
- **The model writes in serif, the app speaks in sans.** Answers, drafts and screen summaries use the serif font, so it's obvious which text came from the model.
- **Evidence is always one click away.** Any `[#id]` in an answer becomes a chip that opens the screenshot.
- **Calm when things fail.** One friendly sentence, never a stack trace or the word "Error:".
- **Works offline.** No web fonts, CDNs or remote icons. System fonts, inline SVG, unicode only.

## Color tokens

Use these exact values, under the same names if your UI has CSS or a theme dictionary. Dark mode follows the OS setting unless the user picks one.

| Token | Light | Dark | Used for |
| --- | --- | --- | --- |
| `--bg` | `#FAF9F5` | `#262624` | Window background |
| `--surface` | `#FFFFFF` | `#30302E` | Cards, composer, modal |
| `--surface2` | `#F5F4EE` | `#1F1E1D` | Sidebar, user bubble, draft box |
| `--recess` | `#F0EEE6` | `#1F1E1D` | Screen-text (mono) box |
| `--line` | `#E8E6DC` | `#3E3E3A` | All borders and dividers (1px) |
| `--text` | `#1F1E1D` | `#F5F4EE` | Body text |
| `--muted` | `#6B6A65` | `#A3A29B` | Metadata, hints, placeholders |
| `--hover` | `rgba(31,30,29,.05)` | `rgba(245,244,238,.06)` | Hover background on rows and ghost buttons |
| `--accent` | `#76B900` | `#76B900` | Green fills: send button, logo dot, checkbox, timeline bars |
| `--on-accent` | `#1F1E1D` | `#1F1E1D` | Text or icon on a green fill (dark, not white) |
| `--accent-text` | `#4D7C0F` | `#A3E635` | Green text and links |
| `--accent-soft` | `rgba(118,185,0,.14)` | `rgba(163,230,53,.13)` | Chip background, selection, focus halo |
| `--warn-text` | `#B45309` | `#F2B35B` | "deadline" pill, "indexing…" |
| `--info-text` | `#1D4ED8` | `#8AB8F5` | "request" pill |
| `--backdrop` | `rgba(31,30,29,.42)` | `rgba(20,20,19,.62)` | Behind modals |

**Rules for green**

- Use `#76B900` for fills only. As text on cream it fails contrast, so use `--accent-text` for green words.
- Text or icons sitting on a green fill are `#1F1E1D`, never white.
- At most one solid green control per window: the main action, such as Send.

## Typography

There are three font stacks, all system fonts, so nothing is downloaded. On the Windows demo laptops this means Segoe UI, Georgia and Consolas.

| Role | Stack | Size / weight | Where |
| --- | --- | --- | --- |
| Sans (chrome) | `-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif` | 15px / 1.5 body; 14px buttons and lists; 12–13px metadata | Buttons, labels, inputs, user messages, menus |
| Serif (model voice) | `"Iowan Old Style", Charter, "Bitstream Charter", Georgia, Cambria, serif` | 17px / 1.65 answers; 16px drafts; 14px card summaries | Anything the model wrote: answers, drafts, standups, screenshot summaries |
| Mono (screen text) | `ui-monospace, SFMono-Regular, Menlo, Consolas, monospace` | 12px / 1.55, muted color | Screen text pulled from a screenshot |

- **Logo:** "Dockt" in sans, 20px, weight 650, letter-spacing −0.02em, followed by a green period (`Dockt.` with the dot in `--accent`).
- **Section labels:** 14px, weight 600, `--muted`, sentence case ("Things you promised", not "THINGS YOU PROMISED").
- **Minimum size:** 15px for anything a judge reads from 2 m. Only metadata goes below 13px.
- The model writes `**bold**`, so render it as bold. No other markdown is rendered.

## Shape, spacing, elevation

Corners are soft, borders are 1px `--line`, and shadow is used only on things that float.

| Element | Radius | Other |
| --- | --- | --- |
| Composer (input box) | 24px | Shadow; border turns `--accent` on focus |
| Modal / popup window | 16px | Shadow; backdrop behind |
| User message bubble | 16px | `--surface2` fill, 10px × 16px padding |
| Cards, draft box, timeline panel | 14px | No shadow; border turns green on hover |
| Buttons, list rows | 10px | 6px × 12px padding |
| Citation chip | 6px | 0 × 6px padding |
| Pills (status, "100% on-device") | 99px | 1px border |
| Send button | Circle, 36px | Solid green |

- **Shadow** (light): `0 1px 2px rgba(31,30,29,.04), 0 6px 20px rgba(31,30,29,.07)`. Dark: `0 1px 2px rgba(0,0,0,.25), 0 8px 24px rgba(0,0,0,.25)`. Used only on the composer, modals and popups.
- **Spacing:** 26px between chat messages, 12px between cards, 8px gaps in button rows, 14–22px padding inside panels.
- **Focus:** a 2px `--accent` outline with a 2px offset on every clickable thing.

## Components

The quick-chat popup needs four of these: composer, answer text, citation chip and loader. Copy them as they are.

- **Composer.** A rounded box (`--surface`, 24px radius, shadow) with a textarea that grows up to 160px, and a 36px round green send button with an up-arrow icon at the bottom right. Enter sends, Shift+Enter adds a new line. Placeholder: "Ask anything you saw…".
- **Answer text.** Serif, 17px, no bubble around it. The user's own message sits right-aligned in a `--surface2` bubble.
- **Citation chip.** Every `[#9]`, `#9` or `[9]` in an answer becomes `#9`: semibold 13px sans, `--accent-soft` background, `--accent-text` color, 6px radius. On hover it turns solid green with dark text. Only create a chip if that id is in the answer's `sources`. Otherwise leave the text alone, because "invoice #4471" is not a capture.
- **Loader.** No spinners. A pulsing 8px green dot followed by a muted hint, e.g. "Searching your screens…". Show the `status` text from `/api/chat` here until the first token arrives.
- **Buttons.** Ghost style: transparent, 1px `--line` border, 10px radius, `--hover` background on hover. Icon-only buttons drop the border.
- **Source card.** A thumbnail (16:10, cropped from the top) above `#id · time · device` metadata, then app — title, then a 3-line serif summary. A cited card gets a green border.
- **Kind pills** (promises list): `promise` is muted, `deadline` is `--warn-text`, `request` is `--info-text`. 11px, outlined, fully rounded.
- **"100% on-device" badge.** 12px green text in an outlined green pill, with a ● dot.
- **Motion.** 0.15s transitions on hover and color only. One exception: a newly found promise row flashes `--accent-soft` for 1.2s.

## Voice and states

Write like a calm colleague: short, plain and specific. Sentence case everywhere. Say "on-device" or "on this laptop" to stress privacy, never "cloud" or "server".

| Situation | Copy the dashboard uses |
| --- | --- |
| Waiting for an answer | "Thinking…", then the agent's `status` text |
| Chat failed or GX10 down | "Couldn't get an answer just now. Check that the GX10 is reachable and try again." |
| Draft failed | "Couldn't write that draft just now. Try again in a moment." |
| Nothing indexed | "No screens captured yet." |
| No promises yet | "Nothing found yet — it shows up here within a minute of being on screen." |
| Drafting | "Drafting on-device…" |
| Footer / tray tooltip | "Stays on this laptop" |

- Never show an error's raw text, HTTP status or stack trace. Log it to the console and show one sentence.
- Each failure tells the user one thing they can do ("try again", "check the GX10").
- Button labels are verbs: "Draft follow-up email", "Write my standup", "New chat".

## Applying it to Lane 1

The popup answers quick questions and the dashboard handles everything else, so every Lane 1 surface should hand off to the dashboard instead of rebuilding it.

**Tray icon**

- Use the favicon shape: a 16×16 rounded square (4px radius) filled `#76B900`.
- Paused (`~/.dockt_pause` exists): grey `#A3A29B`. GX10 unreachable (any `false` from `/api/health`): `#B45309`, with the tooltip "GX10 unreachable".
- Menu labels are sentence case: "Open Dockt", "Pause capture", "Quit".

**Quick-chat popup**

- About 560×420, centered, 16px radius, `--surface` background, using the shadow above. Esc closes it.
- The composer from Components goes at the bottom, focused when the popup opens. The answer goes above it in serif, with chips.
- Show up to 3 source thumbnails (16:10, 14px radius) under the answer.
- Clicking a chip or thumbnail opens `http://127.0.0.1:8000/#/capture/{id}` in the dashboard.
- "Open in Dockt" (ghost button) opens `http://127.0.0.1:8000/#/chat?q=<urlencoded question>`, which continues the question in the full chat.
- Call `POST /api/chat` with `"stream": false` and the full history. On failure, show the friendly line from Voice and states.

**Theme**

- Follow the Windows light/dark setting. The dashboard does the same unless the user flips its toggle, which is saved in the dashboard's own localStorage (`dockt-theme`) and isn't visible to Lane 1.

**Offline and Windows**

- No downloaded fonts or icons. Draw icons as simple strokes (1.8px, round caps) to match the dashboard's inline SVGs.
- Test at 1366×768 and 125% Windows scaling, since that's what the demo laptops are likely to run.
