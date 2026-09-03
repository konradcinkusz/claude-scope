# Session sources other than the JSONL transcript

`scripts/analyze.py` counts from one **normalized event list**. Where that list
comes from is an adapter's problem. This file documents every source other than
Claude Code's on-disk transcript (that one has its own file,
[`transcript-format.md`](transcript-format.md)), in the same style: **observed
on a real session, verified, traps written down.** Nothing here is from
documentation alone.

## The normalized event

```jsonc
{
  "t": "2026-01-02T12:00:00Z",   // ISO-8601, or null if unknown
  "kind": "user",                // see the table below
  "size": 42,                    // characters: prompt, prose, or text read back
  "label": "ship the release notes",  // SHORT. never raw tool output
  "tool": "Read",                // when kind == "tool"
  "is_image": false,             // the tool result was an image, not text
  "model": "some-agent-model",   // when known
  "usage": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
  "capability_tier": "partial"   // per event; a file-level default is fine
}
```

| `kind` | meaning | rendered? |
| --- | --- | --- |
| `user` | a human prompt — **starts a turn** | yes |
| `aside` | short assistant text said mid-work (≤400 chars) | yes |
| `reply` | the assistant's substantive answer (>400 chars) | yes |
| `tool` | one tool call; `size` is what came back | yes |
| `reasoning` | an extended-thinking block — counted, never quoted | no |
| `result` | a tool result arriving back (wall-clock attribution) | no |
| `model` | model activity with no lane content | no |
| `usage` | one response's billing, carried in `usage` | no |

Rules that matter:

- **Usage is summed across every event that carries a `usage` object.** Attach
  it to exactly one event per API response or you will double-count. Both
  `{"input","output","cache_read","cache_write"}` and the raw Anthropic names
  (`input_tokens`, `cache_creation_input_tokens`, …) are accepted.
- Mark subagent/sidechain usage with `"sidechain": true` so it is reported
  separately as well as counted.
- Anything unrecognized — an unknown `kind`, a bad timestamp, a non-object
  entry — is **skipped, never fatal**.
- `label` is human-visible. Never put raw tool output, file contents or whole
  message bodies in it; the analyzer truncates to 160 chars but that is a
  backstop, not a privacy control.

## Capability tiers

A source that cannot reconstruct something must say so rather than render an
empty-looking timeline. The tier flows from the event, through the turn, into
the page.

| tier | has | lacks | renders as |
| --- | --- | --- | --- |
| `full` | per-event time, tool identity + result size, aside/reply split, per-response usage with cache breakdown | — | the full timeline |
| `stream` | all of the above except usage attribution | prompt text and turn boundaries; per-response usage | full lanes, but "You typed" reads `—` and the page says turns are runs |
| `partial` | turn-level totals only | everything inside a turn | one flat block per turn, with the gap stated in the lede, the legend, the tiles and a card |

Pick the **weakest** tier honestly. Under-claiming costs a little detail;
over-claiming silently invents a timeline that was never observed.

---

## Source: `stream-json` (Claude Agent SDK / `claude -p`)

`--stream FILE`. What `claude -p --output-format stream-json --verbose` writes
to stdout, and what any harness embedding the Claude Agent SDK sees. Accepted
as JSONL or as a JSON array.

**Verified** on CLI 2.1.259, 2026-09-03, in a `claude_code_remote` cloud
container, by running two real sessions and cross-checking every number against
the on-disk JSONL for *the same run*: `billed`, the full `usage` breakdown,
`toolCalls`, `reasoningBlocks` and `thinkingTokens` all matched exactly.

### Record types seen

`active_goal`, `autocompact_state`, `system` (subtypes `init`,
`commands_changed`, `thinking_tokens`, `task_summary`, `post_turn_summary`),
`assistant`, `user`, `rate_limit_event`, `result`.

`assistant` and `user` carry the same `message` shape as the on-disk transcript
(`id`, `model`, `content` blocks, `usage`) plus `session_id`, `uuid`,
`parent_tool_use_id`, and — only on these two types — `timestamp`.

### The traps

1. **The human prompt is never emitted.** Not in argv mode (`claude -p "…"`),
   and *not echoed* in `--input-format stream-json` mode either. Both were
   tested. There is no `user` record with string content, so prompt text and
   turn boundaries cannot be recovered from the stream at all. The adapter
   opens each turn with a synthetic, clearly-labelled placeholder rather than
   inventing one, and the report shows `—` for "You typed".
2. **Per-record `usage` is a mid-stream partial — do not sum it.** Same run,
   both formats, same three messages: the stream said `output_tokens` 2, 2, 1;
   the on-disk transcript said 123, 12; `result.usage` said **135**. Summing
   the per-record values under-counts output by ~27×. Authoritative totals live
   only in the terminal `result` record. (Input and cache figures happened to
   match between the formats in this sample, but the safe rule is to take
   everything from `result`.)
3. **Only `assistant` and `user` records have a `timestamp`.** `system`,
   `result` and the control records do not, so turn duration has to come from
   the events themselves.
4. **`num_turns` is not human turns.** It counts API round-trips — a single
   one-sentence prompt produced `num_turns: 2`.
5. `result` is terminal and appeared exactly once per run in both probes; the
   adapter therefore emits **one turn per `result`**, which also generalizes to
   a long-lived harness that keeps the session open.

### Unconfirmed — deliberately not built on

`system/post_turn_summary` looks like a turn boundary and would give real turn
splits. But probe 2 fed two prompts and produced only one `post_turn_summary`
and one `result`, so the 1:1 mapping is **not** established. Splitting turns on
it would be a guess. Left alone; revisit with a session that clearly exercises
multiple human turns.

---

## Source: a normalized event list the agent fetched itself

`--events FILE`. The general path, and the one to use under any agent whose
history lives somewhere `analyze.py` cannot reach — a local database, a cloud
store, or an API only reachable through that agent's own tool calls.

`analyze.py` is stdlib-only by design: it cannot open a database connection or
make a tool call. So the division of labour extends to fetching. **The agent
queries its own store and writes plain JSON; the script still does all the
counting.**

```json
{
  "source": "example-agent",
  "capability_tier": "partial",
  "session_id": "abc12345",
  "events": [
    {"t": "2026-01-02T12:00:00Z", "kind": "user", "size": 42,
     "label": "ship the release notes"},
    {"t": "2026-01-02T12:03:20Z", "kind": "reply", "size": 900,
     "label": "Release notes are ready.",
     "usage": {"input": 300, "output": 1200, "cache_read": 5000, "cache_write": 100}}
  ]
}
```

A bare JSON array of events works too; the tier then defaults to `partial`,
which is the safe assumption.

**Minimum useful input** is one `user` + one `reply` per turn with real
timestamps. That yields turn count, wall-clock per turn, token totals and cache
share — and renders as flat blocks that say what is missing. Add `tool`,
`aside` and `reasoning` events with real timestamps, and raise the tier, only
once you have confirmed the underlying store actually carries them.

**Do not fabricate.** If the store has no per-tool timing, do not scatter tool
events across the turn to make the chart look busy — leave them out and let the
tier say why. An empty band the reader can trust beats a full one they can't.

---

## Checked and found insufficient

Recorded so the next person does not repeat the search.

| source | what it actually holds | verdict |
| --- | --- | --- |
| `mcp__…__get_session` (Claude Code Remote cloud store) | id, title, status, timestamps, environment, model, permission mode, `context_usage` | **No event enumeration.** No `list_events`-style tool in the session's tool surface, and `context_usage.used_tokens` read `0` on a live running session. Not built on. |
| `~/.claude/sessions/<pid>.json` | pid, session id, cwd, CLI version, socket path | pointer only, no history |
| `~/.claude/session-env/<session-id>/` | empty in every session observed | nothing |
| `$CLAUDE_CODE_DIAGNOSTICS_FILE` (`/tmp/claude-code-*.diag.log`) | CLI init timings (`init_started`, `find_git_root_*`, …) | startup telemetry, no dialogue |
| `~/.cache/claude-cli-nodejs/*/mcp-logs-*` | MCP protocol traffic per server | not session history |

Other agents' stores (Copilot CLI, Codex, Gemini, Cursor, aider) could not be
examined: none of those CLIs were installed in this environment, and no
database or history file belonging to them existed on disk. Adapters for them
are **not** written on the strength of documentation — add one only alongside a
live session you have actually inspected.

## Follow-ups

- **Cross-session rollups** (a parent session plus the subagents it spawned).
  Sidechain traffic is already counted in the JSONL path's totals, but no
  verified parent→child *session* link was found in any source here, so no
  rollup is attempted.
- **A cloud-store adapter**, if an event-enumeration tool ever appears
  alongside `get_session`.
- **Turn splitting for `stream-json`**, if `post_turn_summary` turns out to
  track human turns 1:1 on a session that exercises several.
