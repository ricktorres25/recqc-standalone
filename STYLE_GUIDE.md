# RecQC style guide

The visual decisions behind RecQC's page, written down so they can be carried to other MAD tools (the plugins included). Settled with Rick across the design pass of 2026-10-22, starting from the taste skill's app style **data-terminal** (seed 1433756977). The values are plain hex and pixels, so they move to any toolkit. `index.html` is the reference build.

## Rules

1. **Colour goes on data and status, never on chrome.** Frames, labels and controls are neutral. A colour on screen means a measurement or a verdict.
2. **The accent is a line, not a fill.** It marks where you are: the next step, the playback position, the keyboard focus. It also tints the brand mark and, held low, the section edges. It never fills a button or a background.
3. **Dark first; light follows the system setting.** Both themes carry every token below.
4. **Figures in a monospace, words in a sans.** Every number that is read or compared is set in IBM Plex Mono with tabular figures, so columns of digits line up. Everything else is Public Sans.
5. **Surfaces are told apart by one tone step and a thin edge.** Dark mode adds a soft card shadow as its one depth cue; light mode uses none.
6. **Status is a pale pair.** A verdict is a pale tint with a dark ink of the same hue, never a saturated fill with white text.
7. **Motion is feedback only.** Nothing performs an entrance. The one loop (the clap cue's pulse) stops under reduced motion.

## Tokens

| Token | Dark | Light | Job |
|---|---|---|---|
| ground | `#171719` | `#f9fafb` | the page |
| surface | `#131217` | `#ffffff` | cards |
| raised | `#212023` | `#f1f2f4` | tracks, wells, plots, neutral bands |
| border | `#222224` | `#e6e7ea` | quiet outlines (out-of-reach rungs) |
| rule | `#1b1a1f` | `#f0f1f3` | lines inside a card |
| edge | `#2f3f55` | `#c5d2e4` | section edges: cards, the rail divider (the accent's hue, held low) |
| control | `#3a3a3e` | `#cfd1d6` | control and field outlines |
| text | `#ffffff` | `#111111` | primary ink |
| muted | `#8a8c92` | `#5f6368` | secondary ink (passes 4.5:1) |
| accent | `#4a8ad6` | `#1d5fbf` | the line that marks position and selection |
| mark | `#f2f2f3` | `#141414` | data ink: waveforms, value marks, the reached rung |
| positive | `#5fc39c` | `#247b50` | good, as text |
| warning | `#d9a441` | `#92400e` | caution, as text |
| negative | `#e5566f` | `#c4242a` | fail, as text |
| pill positive | `#1b2828` / `#5fc39c` | `#dcfae5` / `#04250e` | background / ink |
| pill warning | `#2a2416` / `#d9a441` | `#fffbeb` / `#92400e` | background / ink |
| pill negative | `#2c1a20` / `#e5566f` | `#ffedea` / `#831c1e` | background / ink |
| pill neutral | `#212023` / `#bdbdc0` | `#eeeff1` / `#3d3d3d` | background / ink |
| primary | `#ffffff` fill, `#0a0a0a` label | `#1d1f20` fill, `#ffffff` label | the one main action |
| card shadow | `0 12px 28px rgba(0,0,0,.2)` | none | dark mode's depth cue |

## Type

Public Sans (variable weight) for words; IBM Plex Mono 400/500 for figures. Both are SIL OFL, vendored in `fonts/`; the licence allows embedding them in a plugin too.

| Role | Size / weight |
|---|---|
| Page title | 26 / 600, tracking -0.01em |
| Lead figure (the answer) | 32 / 500 mono |
| Section title | 20 / 400, muted |
| Step title | 18 / 600 |
| Card title, rung name | 16 / 500-600 |
| Statement (a finding, the phrase) | 18 / 400 |
| Body, controls | 13 / 400 |
| Small, meta, captions | 12 |
| Axis ticks, band labels | 11 (the floor) |

Sentence case everywhere; no tracked capitals.

## Space and shape

- Gap 24 px between blocks; card padding 32 px (16 on a phone).
- Card radius 6; controls are full pills (radius 999); rungs and plot frames 2.
- Controls 34 px tall (44 on a phone, for thumbs).
- The instrument rail is 296 px wide and pinned; under 900 px it folds into a pinned two-row bar.

## Components

**Level meter.** An 18 px track in *raised*. The fill is *positive*, turning *warning* above -3 dBFS and *negative* once clipped. A 2 px peak-hold line in *mark* holds the loudest peak for 1.5 s. The ideal range is a band of *mark* at 14% opacity with 1 px *mark* edges and its name in 11 px inside: ideal peaks -18 to -3 dBFS for speech, clap range -20 to -3 for claps.

**Band rail.** A threshold scale for one measurement: the profile's bands as proportional segments, split by 2 px seams in the surface colour, each named in 11 px. The band the value falls in takes its status pale pair; the others stay *raised*. The value is a 2 px *mark* line overhanging the rail by 4 px. When the line would cross the band's name, the name moves to the free side. Beside it, the margins in mono ("1.2 dB above CAUTION").

**Tier ladder.** Four rungs (Reject, Lo-fi, Good, Hi-fi), 88 px tall. The reached rung is filled with *mark*, its name in bold; rungs above are out of reach, drawn as a 1 px *border* outline; rungs below are *raised*. What caps the result sits on its rung as one pill (full names, or initials on a phone). On a phone the ladder stands upright, highest rung on top.

**Waveform.** A min/max envelope in *mark* at 85% on *raised*, 88 px tall. Reference levels are dashed *muted* lines; events are 3 px ticks along the top edge in status colours; joins are solid *text* lines. The playback pointer is a 2 px *accent* line with a 6 px triangle on top, shown from 0 s and following play, pause and seek.

**Notice.** For a stage that cannot run: the warning (or negative) pale pair with a 1 px inset edge at 45% of its ink, a bold 16 px first line saying what happened, then one sentence saying why. The block below it dims to half opacity.

**Step strip.** One container split by 1 px *rule* lines into equal cells: title, a status pill, the one key figure in mono. The next step carries a 1 px *accent* line above its title, as wide as the title.

**Fact table.** Rows on 1 px *rule* lines, a 12 px *muted* header, no bands or stripes. On a phone each row becomes two lines: the check and its result, then the detail.

**Status words.** PASS, pass, under CLIP and not HOT read positive; CAUTION, QUIET, WARN, production, borderline and HOT read warning; REJECT, reject, TOO_QUIET and CLIP read negative.

## Carrying it to a plugin

- Map the token table onto the plugin's colour scheme (in JUCE, a LookAndFeel colour table), keeping both themes.
- Parameter readouts are figures: set them in the mono with tabular digits.
- A threshold or a detector setting reads well as a band rail: the bands it moves between, a mark at the current value.
- Keep the accent to position and selection (the playhead, the focused control, the active band), and the colour on what the audio did.
