---
name: session-report
description: >-
  Analyze and visualize a Claude Code session from its own transcript: tokens
  billed and cache behaviour, wall-clock per turn, reasoning-vs-tool-execution
  time, who-said-what asymmetry, silent tool-call runs, and a self-contained
  HTML timeline of every prompt, tool call and reply. Use this whenever the
  user asks what a session cost or consumed, how many tokens something used,
  how long the work took, what actually happened in this or an earlier
  session, or wants a session summary, retro, report, stats, timeline or
  visualization of the conversation — even phrased casually ("how many tokens
  did this use?", "sum up this session", "show me what you did"). Prefer this
  skill over hand-parsing ~/.claude/projects JSONL files: it already handles
  the format's traps.
---

# Session Report

Turn a Claude Code session's JSONL transcript into numbers and a picture. The
division of labour is fixed and it matters: **the bundled script does all the
counting; you only label and narrate.** A long session's transcript is
hundreds of kilobytes — reading it into context is slow, expensive, and makes
every number depend on what you happened to sample. The script is
deterministic; your judgment goes where it's actually needed.

## Workflow

### 1. Run the analyzer

```bash
python3 <skill-base-dir>/scripts/analyze.py --json --out .
```

`<skill-base-dir>` is this skill's own directory (it is stated when the skill
loads). By default the script finds the newest transcript for the current
working directory under `~/.claude/projects/`. If the user wants a different
session or the auto-location fails, pass `--transcript /path/to/session.jsonl`
(offer `ls ~/.claude/projects/*/` output to help them pick).

The script prints a compact summary: session totals, then every turn with its
**segments** — spans of work between the moments Claude spoke out loud, each
with a `seg N:SS` key and the opening words that started it. That summary is
your entire input. Do not open the transcript itself; everything you need is
in the summary, and the traps you'd hit in the raw file (duplicate records,
subagent sidechains, CLI bookkeeping pseudo-turns, image payloads inflating
"text read") are already handled.

### 2. Label the phases

Write `labels.json` giving each segment a 2–4 word working label, derived from
its opener quote and the tool activity around it. Verb-first, concrete,
lowercase: "reads both repos", "writes the proposal", "fixes CI", "renders +
inspects". These appear inside the timeline's phase bands, where space is
tight — a label that doesn't fit is hidden and shown on hover, so favor short.

```json
{
  "segments": {
    "1:2": "reads both repos end to end",
    "1:253": "writes ADR 0004",
    "2:17": "reads the house rules"
  },
  "lede": "optional intro sentence for the page header (HTML allowed)",
  "cards": "optional HTML overriding the four insight cards",
  "title": "optional page title"
}
```

Everything except `segments` is optional — the script generates a sound
default lede, stat tiles and insight cards from the data alone. Override
`cards` only when the session has a story the defaults miss (write them as
`<div class="note"><h3>…</h3><p><span class="big">…</span><br>…</p></div>`).

### 3. Render and deliver

```bash
python3 <skill-base-dir>/scripts/analyze.py --labels labels.json --out .
```

This writes `session-report-<id>.html` — fully self-contained, both light and
dark themes, hover tooltips, and a full event table. Deliver it the best way
the environment allows: publish it as an artifact if an artifact tool is
available, otherwise send the file to the user or give its path.

### 4. Report the TL;DR in chat

Lead with the headline numbers (tokens billed, cache share, wall-clock,
tool calls, the reasoning-vs-execution split), then the one or two findings
that would change behaviour — a long silent run, a single read that dwarfed
the conversation, a cold-cache resume after a gap. Numbers from the usage
fields are exact; anything converted from characters to tokens is an
estimate and must be labelled as one.

## Privacy

The transcript contains the full conversation **and every tool output** —
which can include secrets, credentials and private data. The report keeps to
aggregates, labels and short opener quotes; keep it that way. Never quote
tool outputs into the report or the chat summary, and remember the HTML file
is the kind of thing users share onward.

## Flags

| Flag | Meaning |
| --- | --- |
| `--transcript PATH` | analyze this file instead of auto-locating |
| `--cwd PATH` | project directory used for auto-location (default `$PWD`) |
| `--out DIR` | where to write outputs (default `.`) |
| `--json` | also write `session-report-<id>.json` with full turns + metrics |
| `--labels FILE` | labels/overrides file from step 2 |
| `--no-html` | numbers only, skip rendering |

## When things look wrong

- **"no transcripts under …"** — the project path munging may not match this
  platform, or the session lives elsewhere. Ask the user, or glob
  `~/.claude/projects/*/*.jsonl` by mtime and pass `--transcript`.
- **A turn shows `[in flight]`** — expected: the current turn has no reply
  yet, and re-running mid-session moves the numbers. Say the report is a
  snapshot.
- **Parsing looks off after a CLI update** — the transcript format is
  undocumented and does change. Read
  `references/transcript-format.md` (the observed format and every known
  trap) before editing `scripts/analyze.py`, and keep parsing defensive:
  skip what you don't recognize, never crash.
