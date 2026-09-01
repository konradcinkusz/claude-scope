# claude-scope

Session forensics for Claude Code: a `/session-report` skill that turns a
session's own transcript into token, timing and conversation-shape analytics —
with a visual timeline. Installable as a plugin.

Ask *"how many tokens did this session use?"* or *"sum up what you did"* and
you get back a page like this: every prompt, tool call and reply placed at the
second it happened, phases of work named by what was actually going on, and
the numbers that explain where the time and tokens went.

## What it measures

- **Tokens billed** — exact, from the API usage fields: fresh input, cache
  writes, cache reads, output — deduplicated per API response, subagent
  traffic included.
- **Time** — wall-clock per turn, and the split between *model reasoning*
  and *tool execution* (in the sessions this was built on, reasoning beat
  execution roughly 4:1).
- **Conversation shape** — the asymmetry between what you typed and what the
  agent did (hundreds of characters in, tens of millions of tokens through),
  how much of the agent's prose arrived only in the final reply of each turn,
  and the longest run of tool calls with nothing said out loud.
- **The timeline** — one card per turn on a shared clock: phase bands opening
  where Claude narrated, tool-call ticks scaled by how much text each read
  back, and the reply block that ends the turn. Self-contained HTML, light
  and dark, hover tooltips, full event table.

## Install

```sh
claude plugin marketplace add konradcinkusz/claude-scope
claude plugin install claude-scope@claude-scope
```

Then, in any session: `/session-report` — or just ask naturally ("what did
this session cost?", "show me a timeline of this conversation").

## How it works

Claude Code writes every session to a JSONL transcript under
`~/.claude/projects/`. A bundled Python script (stdlib only) parses it
**deterministically** — the model never reads the raw transcript. The script
counts; the model only names the phases of work (from its own narration,
which turns out to segment sessions naturally) and writes the summary. That
split keeps the numbers exact and the analysis cheap: analyzing a session
costs a small fraction of the session itself.

The transcript format is undocumented, and it bites: duplicate records per
API response, subagent sidechains, pseudo-turns fabricated by local slash
commands, image payloads masquerading as huge text reads. The parser handles
each of these; they're documented in
[`plugins/claude-scope/skills/session-report/references/transcript-format.md`](plugins/claude-scope/skills/session-report/references/transcript-format.md),
which is the closest thing to a spec we're aware of.

## What it is not

- Not a cost dashboard across days and projects — [ccusage](https://github.com/ryoppippi/ccusage)
  and Claude Code's own `/usage` already do that well. This is a *retro of
  one session*: what happened, in what order, at what cost.
- Not a monitor. It runs when you ask, on a snapshot.

## Privacy

Transcripts contain your full conversation **and every tool output** —
possibly secrets. The report is built from aggregates, phase labels and
short quoted narration; tool outputs are never copied into it. Treat the
generated HTML as shareable only to the extent your session was.

## Caveats

- The transcript format is unstable between CLI releases; parsing is
  defensive (skip, never crash), but a format change can hide data until the
  parser catches up. Issues welcome with a redacted sample line.
- Transcript locations are as observed on Linux/macOS local and remote
  environments; pass `--transcript` explicitly where auto-location misses.
- Character→token conversions shown anywhere are estimates; usage-field
  numbers are exact.

## License

MIT — see [LICENSE](LICENSE).
