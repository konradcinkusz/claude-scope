---
name: session-report
description: >-
  Analyze and visualize a coding-agent session from its own session data:
  tokens billed and cache behaviour, wall-clock per turn, reasoning-vs-tool-
  execution time, who-said-what asymmetry, silent tool-call runs, and a
  self-contained HTML timeline of every prompt, tool call and reply. Use this
  whenever the user asks what a session cost or consumed, how many tokens
  something used, how long the work took, what actually happened in this or an
  earlier session, or wants a session summary, retro, report, stats, timeline
  or visualization of the conversation — even phrased casually ("how many
  tokens did this use?", "sum up this session", "show me what you did"). Works
  under Claude Code from its ~/.claude/projects JSONL transcript, and under
  other agents from whatever session data they expose. Prefer this skill over
  hand-parsing transcripts: it already handles the formats' traps.
---

# Session Report

Turn a coding session into numbers and a picture. The division of labour is
fixed and it matters: **the bundled script does all the counting; you only
fetch, label and narrate.** A long session's transcript is hundreds of
kilobytes — reading it into context is slow, expensive, and makes every number
depend on what you happened to sample. The script is deterministic; your
judgment goes where it's actually needed.

The script is stdlib-only Python. It can read files; it cannot open a database
or make a tool call. So when the session data lives somewhere only *you* can
reach, you fetch it and write plain JSON — and the script still does the
counting.

## Workflow

### 1. Get the data in front of the script

Try these in order and stop at the first that works.

**a. Claude Code's own transcript (the common case).** Just run it — the
script finds the newest transcript for the current working directory under
`~/.claude/projects/`:

```bash
python3 <skill-base-dir>/scripts/analyze.py --json --out .
```

`<skill-base-dir>` is this skill's own directory (it is stated when the skill
loads). If the user wants a different session, pass `--transcript
/path/to/session.jsonl` (offer `ls ~/.claude/projects/*/` output to help them
pick).

**b. A stream-json capture.** If the session was driven through the Claude
Agent SDK or `claude -p --output-format stream-json`, point the script at that
capture instead:

```bash
python3 <skill-base-dir>/scripts/analyze.py --stream /path/to/stream.jsonl --json --out .
```

**c. Anything else — discover, fetch, then analyze.** If step (a) fails with
`no transcripts under …`, you are not running under Claude Code, or not where
it keeps its history. Do not give up and do not guess a schema. Find out what
*this* agent can actually see:

- Are there agent-specific tools for listing session events or history?
  Search your available tools before concluding there aren't.
- Is there a documented local store — a database, a history directory, a
  config pointing at one?
- Do environment variables name a session id, a container, a data directory?

Then **query a real, current session and look at what comes back.** Verify
fields against live data; do not build on documentation alone. Write what you
found as a normalized event list and run the script against it:

```bash
python3 <skill-base-dir>/scripts/analyze.py --events /tmp/session-events.json --json --out .
```

The file format, the `kind` vocabulary, the capability tiers and the rules
about not fabricating detail are in `references/session-sources.md`. Read it
before writing the file. Two things matter most:

- **Claim the weakest tier that is honestly true.** If the store gives you
  turn-level totals only, emit one `user` + one `reply` per turn and set
  `"capability_tier": "partial"`. The report then renders flat blocks and says
  what was missing — which is the point. Scattering invented tool events to
  make the chart look busy is worse than an empty band.
- **Never put raw tool output, file contents or whole message bodies in
  `label`.** See Privacy below.

If you discover a source worth supporting properly, add an adapter to
`scripts/analyze.py` and document it in `references/session-sources.md` — with
the same rigor: observed on a real session, verified, traps written down.

### 2. Read the summary

The script prints a compact summary: session totals, then every turn with its
**segments** — spans of work between the moments the assistant spoke out loud,
each with a `seg N:SS` key and the opening words that started it. If the source
was below `full` tier, the summary says so and names what was missing.

That summary is your entire input. Do not open the transcript itself;
everything you need is in the summary, and the traps you'd hit in the raw file
(duplicate records, subagent sidechains, CLI bookkeeping pseudo-turns, image
payloads inflating "text read", mid-stream partial token counts) are already
handled.

### 3. Label the phases

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

A `partial`-tier session has no segments to label. Leave `segments` out; the
defaults already explain the gap.

### 4. Render and deliver

```bash
python3 <skill-base-dir>/scripts/analyze.py --labels labels.json --out .
```

(with the same `--transcript` / `--stream` / `--events` flag you used in step
1). This writes `session-report-<id>.html` — fully self-contained, both light
and dark themes, hover tooltips, and a full event table. Deliver it the best
way the environment allows: publish it as an artifact if an artifact tool is
available, otherwise send the file to the user or give its path.

### 5. Report the TL;DR in chat

Lead with the headline numbers (tokens billed, cache share, wall-clock,
tool calls, the reasoning-vs-execution split), then the one or two findings
that would change behaviour — a long silent run, a single read that dwarfed
the conversation, a cold-cache resume after a gap. Numbers from the usage
fields are exact; anything converted from characters to tokens is an
estimate and must be labelled as one. If the source was below `full` tier,
say so in one clause — don't present a partial reconstruction as a complete
one.

## Privacy

Session data contains the full conversation **and every tool output** — which
can include secrets, credentials and private data. The report keeps to
aggregates, labels and short opener quotes; keep it that way, for every source
you add. Never quote tool outputs into the report or the chat summary, never
put message bodies into an events file you write, and never surface
extended-reasoning text (it is counted, never quoted). Remember the HTML file
is the kind of thing users share onward.

## Flags

| Flag | Meaning |
| --- | --- |
| `--transcript PATH` | analyze this Claude Code `.jsonl` instead of auto-locating |
| `--stream PATH` | analyze a stream-json capture |
| `--events PATH` | analyze a normalized event list you fetched and wrote |
| `--cwd PATH` | project directory used for auto-location (default `$PWD`) |
| `--out DIR` | where to write outputs (default `.`) |
| `--json` | also write `session-report-<id>.json` with full turns + metrics |
| `--labels FILE` | labels/overrides file from step 3 |
| `--no-html` | numbers only, skip rendering |

## When things look wrong

- **"no transcripts under …"** — you may not be under Claude Code at all. Go
  to step 1c: discover what this agent exposes, fetch it, use `--events`.
  Under Claude Code, the project path munging may not match this platform —
  glob `~/.claude/projects/*/*.jsonl` by mtime and pass `--transcript`.
- **A turn shows `[in flight]`** — expected: the current turn has no reply
  yet, and re-running mid-session moves the numbers. Say the report is a
  snapshot.
- **"You typed —" instead of a number** — the source didn't expose prompt
  text. That's honest reporting, not a bug; `stream-json` never carries it.
- **Flat blocks instead of a timeline** — the source is `partial` tier. The
  page says what was missing. Don't try to fill it in by hand.
- **Parsing looks off after a CLI update** — these formats are undocumented
  and do change. Read `references/transcript-format.md` (the JSONL format and
  every known trap) and `references/session-sources.md` (every other source)
  before editing `scripts/analyze.py`, and keep parsing defensive: skip what
  you don't recognize, never crash.
- **Changing the analyzer** — `tests/` in the repo root covers the traps and
  pins the JSONL path's rendered shape. Run `python3 tests/test_analyze.py`.
