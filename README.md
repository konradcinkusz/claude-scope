# claude-scope

Session forensics for coding agents: a `/session-report` skill that turns a
session's own history into token, timing and conversation-shape analytics —
with a visual timeline. Built for Claude Code's transcript, with an adapter
boundary so it can report on whatever session data the agent running it can
actually reach. Installable as a plugin.

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

**Claude Code**

```sh
claude plugin marketplace add konradcinkusz/claude-scope
claude plugin install claude-scope@claude-scope
```

Then, in any session: `/session-report` — or just ask naturally ("what did
this session cost?", "show me a timeline of this conversation").

**GitHub Copilot CLI** — the plugin follows the vendor-neutral
[Agent Plugins](https://agent-plugins.org/) format, so the same tree installs
there too:

```sh
copilot plugin marketplace add konradcinkusz/claude-scope
copilot plugin install claude-scope@claude-scope
```

**VS Code** — add the repository to `chat.plugins.marketplaces`:

```json
{ "chat.plugins.marketplaces": ["konradcinkusz/claude-scope"] }
```

The skill installs and runs anywhere, and it is no longer tied to one
transcript format. The analyzer counts from a **normalized event list**; where
that list comes from is an adapter's problem. Three exist today:

| flag | source | tier |
| --- | --- | --- |
| *(default)* / `--transcript` | Claude Code's `~/.claude/projects/*.jsonl` | `full` |
| `--stream` | a stream-json capture (Claude Agent SDK, `claude -p`) | `stream` |
| `--events` | a normalized event list the agent fetched from its own store | whatever it honestly claims |

That last one is the general escape hatch. The script is stdlib-only and cannot
open a database or make a tool call — so under an agent whose history lives in
a local database or a cloud store, **the agent queries its own store and writes
plain JSON, and the script still does all the counting.** The rule that the
model never does arithmetic extends to fetching: it supplies raw facts, the
script computes.

Every source declares a **capability tier**, and the tier reaches the page. A
source that only exposes turn-level totals renders one flat block per turn and
says so — in the lede, the legend, the stat tiles and a card — instead of
drawing an empty-looking timeline. Under-claiming costs a little detail;
over-claiming invents a session that was never observed.

## How it works

Claude Code writes every session to a JSONL transcript under
`~/.claude/projects/`. A bundled Python script (stdlib only) parses it
**deterministically** — the model never reads the raw transcript. The script
counts; the model only names the phases of work (from its own narration,
which turns out to segment sessions naturally) and writes the summary. That
split keeps the numbers exact and the analysis cheap: analyzing a session
costs a small fraction of the session itself.

Under another agent, the model's extra job is *fetching* — query the session
store, write the normalized JSON, hand it to the script. It still does no
arithmetic.

These formats are undocumented, and they bite: duplicate records per API
response, subagent sidechains, pseudo-turns fabricated by local slash commands,
image payloads masquerading as huge text reads, extended-reasoning blocks whose
text arrives redacted to `""`, and — in stream-json — per-record token counts
that are mid-stream partials and undercount output by more than an order of
magnitude if you sum them. Every one of these was hit on a real session. The
parser handles them; they're documented in
[`transcript-format.md`](plugins/claude-scope/skills/session-report/references/transcript-format.md)
(the JSONL format) and
[`session-sources.md`](plugins/claude-scope/skills/session-report/references/session-sources.md)
(every other source, plus the ones we checked and found insufficient), which
are the closest thing to a spec we're aware of.

Run `python3 tests/test_analyze.py` after touching the analyzer — the suite
pins each trap and the rendered shape of the JSONL path.

## What it is not

- Not a cost dashboard across days and projects — [ccusage](https://github.com/ryoppippi/ccusage)
  and Claude Code's own `/usage` already do that well. This is a *retro of
  one session*: what happened, in what order, at what cost.
- Not a monitor. It runs when you ask, on a snapshot.

## Privacy

Session data contains your full conversation **and every tool output** —
possibly secrets. The report is built from aggregates, phase labels and short
quoted narration; tool outputs are never copied into it, and extended-reasoning
text is counted but never quoted. The same rule binds any new source. Treat the
generated HTML as shareable only to the extent your session was.

## Caveats

- These formats are unstable between releases; parsing is defensive (skip,
  never crash), but a format change can hide data until the parser catches up.
  Issues welcome with a redacted sample line.
- Transcript locations are as observed on Linux/macOS local and remote
  environments; pass `--transcript` explicitly where auto-location misses.
- Adapters are only written for sources verified against a live session. Where
  a store looked promising but couldn't be confirmed, it's recorded as such in
  `session-sources.md` rather than guessed at.
- Character→token conversions shown anywhere are estimates; usage-field
  numbers are exact.

## License

MIT — see [LICENSE](LICENSE).
