# The Claude Code session transcript, as observed

The CLI writes one JSONL file per session:
`~/.claude/projects/<munged-cwd>/<session-id>.jsonl`, where `<munged-cwd>` is
the project working directory with `/` and `.` replaced by `-`
(`/home/user` → `-home-user`).

**This format is undocumented and unstable.** Everything below was observed
on real sessions (CLI 2.x, mid-2026, local and remote/web environments) and
verified against API usage totals; any CLI release may change it. Parse
defensively: unknown record types and missing keys are skipped, never fatal.

## Record types seen

One JSON object per line. `type` is one of:

- `assistant` — a slice of an API response. Carries `message` (an API
  message: `id`, `model`, `role`, `content` blocks, `usage`), plus
  `timestamp`, `uuid`, `parentUuid`, `requestId`, `isSidechain`.
- `user` — either a real human prompt (`message.content` is a **string**)
  or tool results going back to the model (`message.content` is a **list**
  of `tool_result` blocks). Real prompts also carry `promptId`.
- `attachment`, `system`, `queue-operation`, `mode`, `atis-latch`,
  `last-prompt` — harness bookkeeping; ignore, but tolerate more.

## The traps (each one cost us a bug)

1. **One API response spans several `assistant` records** sharing
   `message.id` — e.g. a text block and a tool_use block arrive as separate
   lines, each with the full `usage`. Count usage **once per message id**
   (first record wins) and dedupe content blocks by
   `(message.id, block type, block id or text prefix)`.
2. **Local slash commands fabricate user turns.** A `/model` switch wrote
   four string-content user records: `<local-command-caveat>…`,
   `<command-name>…`, `<local-command-stdout>…`, `<system-reminder>…`.
   They are not dialogue. Filter string prompts starting with those
   prefixes (drop the pseudo-turn entirely when it gathered no assistant
   activity).
3. **Subagents write into the same file** with `isSidechain: true`. Their
   usage is real billed usage (count it in totals) but their events are not
   part of the main conversation's lanes.
4. **Tool results can be images.** `tool_result.content` may be a list
   containing `{"type": "image", …}` blocks whose base64 serializes to
   hundreds of KB. Detect the image block and flag it — otherwise one
   screenshot dwarfs every text read in any size-derived scale.
5. **The trailing line may be half-written** while the session is live.
   Tolerate JSON decode errors per line.
6. **`usage` shape**: `input_tokens` (uncached), `cache_creation_input_tokens`,
   `cache_read_input_tokens`, `output_tokens`, plus nested detail
   (`cache_creation.ephemeral_1h_input_tokens` / `_5m_`,
   `output_tokens_details.thinking_tokens`, `iterations[]`). Billed total =
   the four top-level counters summed across deduped responses. A model or
   cache-TTL change mid-session shows up as a cache_creation spike after a
   gap — that's a cold resume, not a bug.
7. **Timestamps** are ISO-8601 UTC with `Z`. Wall-clock attribution:
   RESULT→MODEL gap ≈ model latency (thinking + generation), MODEL→RESULT
   gap ≈ tool execution; cap single gaps (10 min) so idle time between
   turns isn't attributed to either.
8. **`message.model` varies per response** — model switches (`/model`) and
   serving fallbacks are visible here and only here.

## Semantic conventions the analyzer derives

- A **turn** = one real user prompt plus everything until the next one.
- Assistant text ≤ 400 chars = an **aside** (status narration); longer =
  the turn's **reply**. In practice ~90% of assistant prose sits in the
  final reply block, and asides mark phase boundaries.
- A **segment** = the span between consecutive asides (or start/reply):
  the phases of work the timeline's bands show. The aside that opens a
  segment is quoted as its `opener`.
