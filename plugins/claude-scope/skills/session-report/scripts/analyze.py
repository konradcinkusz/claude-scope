#!/usr/bin/env python3
"""claude-scope session analyzer.

Computes token / timing / conversation-shape statistics for one coding-agent
session deterministically, and renders a self-contained HTML timeline from the
bundled template.

The counting logic consumes ONE normalized event list (see `event()` below).
Where that list came from is an adapter's problem:

  --transcript FILE  Claude Code's own JSONL transcript (the default; the
                     newest file under ~/.claude/projects/<munged-cwd>/)
  --stream FILE      a stream-json capture, i.e. what
                     `claude -p --output-format stream-json` writes and what
                     any harness embedding the Claude Agent SDK sees
  --events FILE      a normalized event list some OTHER agent pre-fetched from
                     its own store and wrote to plain JSON

That last one is the general escape hatch. This script is stdlib-only and
cannot open a database or make a tool call; when the executing agent's history
lives somewhere only that agent can reach, the agent fetches it and writes the
JSON, and the script still does all the counting. See
../references/session-sources.md for the contract.

Not every source exposes the same detail, so every event carries a
`tier` (its capability tier) which flows through to rendering — a source that
cannot reconstruct intra-turn narration renders one honest flat block per turn
instead of an empty-looking timeline.

Design rule: this script does ALL counting. The model reading its output only
labels phases and writes narrative. Never feed a raw transcript to the model -
a long session is hundreds of KB and the numbers must not depend on sampling.

Every format handled here is undocumented and can change, so parsing is
deliberately defensive: unknown record types and missing keys are skipped,
never fatal. See ../references/transcript-format.md (JSONL) and
../references/session-sources.md (everything else) for what each handled shape
means and which real sessions it was observed in.

Stdlib only. Python 3.9+.
"""
import argparse, datetime as dt, itertools, json, os, re, sys
from pathlib import Path

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens", "output_tokens")
# A user record whose string content starts with one of these is CLI
# bookkeeping (local slash commands, model switches), not dialogue.
WRAPPER_PREFIXES = ("<local-command-", "<command-name>", "<system-reminder>",
                    "Caveat:", "<local-command-stdout>")
ASIDE_MAX_CHARS = 400          # text blocks longer than this count as the turn's reply
GAP_CAP_S = 600                # ignore inter-record gaps longer than this in time attribution
COLD_GAP_MIN = 30              # report session gaps longer than this many minutes

# ---- the normalized event model ------------------------------------------
#
# Adapters emit a flat list of these; analyze() consumes nothing else.
#
#   t     datetime or None   when it happened
#   kind  see below
#   size  int                characters (prompt, prose, tool output read back)
#   label str                short human-visible text; never raw tool output
#   tool  str or None        tool name, when kind == "tool"
#   is_image bool            the tool result was an image, not text
#   model str or None        model id that produced it, when known
#   usage dict or None       API usage for ONE response, in USAGE_KEYS names
#   tier  str                capability tier of the source that emitted it
#
# Kinds that appear in the lanes and the event table:
LANE_KINDS = ("user", "aside", "reply", "tool")
# Kinds that exist for accounting only and are never rendered:
#   reasoning  an extended-thinking block (counted, never quoted)
#   result     a tool result arriving back (wall-clock attribution only)
#   model      model activity with no lane content (empty/unhandled block)
#   usage      one API response's billing, carried by `usage`
KNOWN_KINDS = LANE_KINDS + ("reasoning", "result", "model", "usage")

# How each kind marks the wall-clock timeline. MODEL->RESULT gaps are tool
# execution, RESULT->MODEL gaps are model latency.
MARKERS = {"user": "USER", "result": "RESULT", "aside": "MODEL",
           "reply": "MODEL", "tool": "MODEL", "reasoning": "MODEL",
           "model": "MODEL"}

# Exactly which keys, in which order, each lane kind contributes to the
# rendered payload. Pinned rather than derived so that adding fields to the
# normalized model can never change a rendered report.
LANE_KEYS = {"aside": ("kind", "label", "size"),
             "reply": ("kind", "label", "size"),
             "tool": ("kind", "tool", "label", "size", "is_image")}

TIER_FULL = "full"        # everything: per-event time, tool identity, per-response usage
TIER_STREAM = "stream"    # per-event time and tool identity, but no prompts and
                          # no per-response usage attribution
TIER_PARTIAL = "partial"  # turn-level aggregates only; no intra-turn structure
TIER_RANK = {TIER_FULL: 2, TIER_STREAM: 1, TIER_PARTIAL: 0}
# Below TIER_STREAM a turn has no reconstructable inside, so it renders flat.
TIER_HAS_DETAIL = {TIER_FULL: True, TIER_STREAM: True, TIER_PARTIAL: False}
# Short, honest statement of what the source could not reconstruct.
TIER_GAP = {
    TIER_STREAM: "this source records what the assistant did but not the "
                 "prompts that set it off, so turns are runs, not questions, "
                 "and token totals are per run rather than per response",
    TIER_PARTIAL: "this source exposes turn-level totals only, so the phases "
                  "of work and individual tool calls inside each turn could "
                  "not be reconstructed",
}
# Neutral usage names accepted from foreign agents, mapped to the API's.
NEUTRAL_USAGE = {"input": "input_tokens", "output": "output_tokens",
                 "cache_read": "cache_read_input_tokens",
                 "cache_write": "cache_creation_input_tokens"}


def event(t, kind, size=0, label="", tool=None, is_image=False, model=None,
          usage=None, tier=TIER_FULL, **extra):
    e = {"t": t, "kind": kind, "size": size, "label": label, "tool": tool,
         "is_image": is_image, "model": model, "usage": usage, "tier": tier}
    e.update(extra)
    return e


def weakest(a, b):
    return a if TIER_RANK.get(a, 0) <= TIER_RANK.get(b, 0) else b


def parse_ts(rec):
    return parse_iso(rec.get("timestamp"))


def parse_iso(t):
    if not t or not isinstance(t, str):
        return None
    try:
        return dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
    except ValueError:
        return None


def munge_cwd(cwd):
    # Observed rule: '/' and '.' become '-' ('/home/user' -> '-home-user').
    return re.sub(r"[/.]", "-", str(cwd))


def locate_transcript(cwd, explicit=None):
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            sys.exit(f"error: transcript not found: {p}")
        return p
    proj = Path.home() / ".claude" / "projects" / munge_cwd(cwd)
    candidates = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                        reverse=True)
    if not candidates:
        sys.exit(f"error: no transcripts under {proj}\n"
                 f"Pass one explicitly with --transcript PATH, or fetch this "
                 f"agent's own session data and pass --events PATH "
                 f"(see references/session-sources.md).")
    return candidates[0]


def load_records(path):
    """JSONL, or a JSON array of records, whichever the file turns out to be."""
    text = Path(path).read_text(encoding="utf-8")
    stripped = text.lstrip()
    if stripped.startswith("["):
        try:
            arr = json.loads(stripped)
            return [r for r in arr if isinstance(r, dict)]
        except json.JSONDecodeError:
            pass
    recs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a partially-written trailing line is normal mid-session
    return recs


def result_info(block):
    """(serialized length, is_image) for one tool_result content block."""
    c = block.get("content")
    if isinstance(c, str):
        return len(c), False
    if isinstance(c, list):
        is_img = any(isinstance(b, dict) and b.get("type") == "image" for b in c)
        try:
            return len(json.dumps(c)), is_img
        except (TypeError, ValueError):
            return 0, is_img
    return 0, False


def word_trim(text, limit=26):
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit + 4]
    return cut.rsplit(" ", 1)[0] + "…"


def tool_label(block):
    inp = block.get("input") or {}
    if not isinstance(inp, dict):
        inp = {}
    return str(inp.get("description") or inp.get("title") or inp.get("query")
               or inp.get("prompt") or block.get("name") or "tool")[:160]


def tool_results(recs):
    """tool_use_id -> (chars read back, was an image). Sidechains included."""
    out = {}
    for r in recs:
        if r.get("type") != "user":
            continue
        c = (r.get("message") or {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    out[b.get("tool_use_id")] = result_info(b)
    return out


# ---- adapters ------------------------------------------------------------

def events_from_jsonl(recs):
    """Claude Code's own transcript. Full tier.

    One API response spans several assistant records sharing message.id; count
    its usage once (first record wins) and each content block once.
    """
    results = tool_results(recs)
    events, seen_msgs, seen_blocks = [], set(), set()

    for r in recs:
        ts = parse_ts(r)
        side = bool(r.get("isSidechain"))
        rtype = r.get("type")
        msg = r.get("message") or {}
        if rtype == "assistant":
            mid = msg.get("id")
            if mid is not None and mid not in seen_msgs:
                seen_msgs.add(mid)
                events.append(event(ts, "usage", usage=msg.get("usage") or {},
                                    model=msg.get("model"), sidechain=side))
            if side:
                continue  # subagent traffic: billed (above) but not a lane event
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                key = (mid, b.get("type"), b.get("id") or (b.get("text") or "")[:50])
                if key in seen_blocks:
                    continue
                seen_blocks.add(key)
                btype = b.get("type")
                if btype == "text" and (b.get("text") or "").strip():
                    txt = b["text"]
                    events.append(event(ts, "reply" if len(txt) > ASIDE_MAX_CHARS
                                        else "aside",
                                        label=" ".join(txt.split())[:160],
                                        size=len(txt), model=msg.get("model")))
                elif btype == "tool_use":
                    size, is_img = results.get(b.get("id"), (0, False))
                    events.append(event(ts, "tool", tool=b.get("name"),
                                        label=tool_label(b), size=size,
                                        is_image=is_img))
                elif btype == "thinking":
                    # Extended reasoning. Counted, never quoted - and often
                    # redacted to "" on the wire, so size can honestly be 0.
                    events.append(event(ts, "reasoning",
                                        size=len(b.get("thinking") or "")))
                else:
                    # Unknown or empty block: still model activity on the clock.
                    events.append(event(ts, "model"))
        elif rtype == "user" and not side:
            c = msg.get("content")
            if isinstance(c, str):
                events.append(event(ts, "user", label=" ".join(c.split()),
                                    size=len(c),
                                    wrapper=c.startswith(WRAPPER_PREFIXES)))
            elif isinstance(c, list):
                events.append(event(ts, "result"))
    return events


def events_from_stream(recs):
    """A stream-json capture (Claude Agent SDK / `claude -p`). Stream tier.

    Two verified differences from the on-disk transcript drive this adapter:
    the human prompt is never emitted (not in argv mode, not echoed in
    --input-format stream-json either), and the per-record `usage` is a
    mid-stream partial - authoritative totals live only in the terminal
    `result` record. So: one turn per `result`, opened by a synthetic prompt,
    and usage taken from `result` alone.
    """
    results = tool_results(recs)
    events, seen_blocks, run = [], set(), []

    def close_run(usage=None, model=None):
        """Emit one turn's worth of events, opened by a synthetic prompt."""
        if usage is not None:
            events.append(event(None, "usage", usage=usage, model=model,
                                tier=TIER_STREAM))
        stamped = [e for e in run if e["t"] is not None]
        if stamped:
            events.append(event(min(e["t"] for e in stamped), "user",
                                label="(prompt not recorded by this source)",
                                size=0, tier=TIER_STREAM, synthetic=True))
        events.extend(run)
        del run[:]

    for r in recs:
        rtype = r.get("type")
        ts = parse_ts(r)
        msg = r.get("message") or {}
        if rtype == "assistant":
            mid = msg.get("id")
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                key = (mid, b.get("type"), b.get("id") or (b.get("text") or "")[:50])
                if key in seen_blocks:
                    continue
                seen_blocks.add(key)
                btype = b.get("type")
                if btype == "text" and (b.get("text") or "").strip():
                    txt = b["text"]
                    run.append(event(ts, "reply" if len(txt) > ASIDE_MAX_CHARS
                                     else "aside",
                                     label=" ".join(txt.split())[:160],
                                     size=len(txt), model=msg.get("model"),
                                     tier=TIER_STREAM))
                elif btype == "tool_use":
                    size, is_img = results.get(b.get("id"), (0, False))
                    run.append(event(ts, "tool", tool=b.get("name"),
                                     label=tool_label(b), size=size,
                                     is_image=is_img, tier=TIER_STREAM))
                elif btype == "thinking":
                    run.append(event(ts, "reasoning",
                                     size=len(b.get("thinking") or ""),
                                     tier=TIER_STREAM))
                else:
                    run.append(event(ts, "model", tier=TIER_STREAM))
        elif rtype == "user":
            c = msg.get("content")
            if isinstance(c, str) and c.strip():
                # Not emitted by the CLI, but a harness may inject one; take it.
                run.append(event(ts, "user", label=" ".join(c.split()),
                                 size=len(c), tier=TIER_STREAM))
            elif isinstance(c, list):
                run.append(event(ts, "result", tier=TIER_STREAM))
        elif rtype == "result":
            models = [m for m in (r.get("modelUsage") or {})]
            close_run(r.get("usage") or {}, models[0] if models else None)
    close_run()
    return events


def events_from_normalized(doc):
    """A normalized event list some other agent pre-fetched into JSON.

    The general path for agents whose history is only reachable through their
    own tool calls. Contract in ../references/session-sources.md. Every field
    is optional and anything unrecognized is skipped, never fatal.
    """
    if isinstance(doc, list):
        doc = {"events": doc}
    if not isinstance(doc, dict):
        return []
    default_tier = doc.get("capability_tier") or doc.get("tier") or TIER_PARTIAL
    if default_tier not in TIER_RANK:
        default_tier = TIER_PARTIAL
    out = []
    for raw in doc.get("events") or []:
        if not isinstance(raw, dict):
            continue
        kind = raw.get("kind")
        if kind not in KNOWN_KINDS:
            continue
        tier = raw.get("capability_tier") or raw.get("tier") or default_tier
        if tier not in TIER_RANK:
            tier = default_tier
        usage = raw.get("usage")
        if isinstance(usage, dict):
            usage = {NEUTRAL_USAGE.get(k, k): v for k, v in usage.items()}
        else:
            usage = None
        try:
            size = int(raw.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        label = raw.get("label") or ""
        out.append(event(parse_iso(raw.get("t")), kind, size=size,
                         label=" ".join(str(label).split())[:160],
                         tool=raw.get("tool"),
                         is_image=bool(raw.get("is_image")),
                         model=raw.get("model"), usage=usage, tier=tier,
                         sidechain=bool(raw.get("sidechain"))))
    return out


# ---- analysis ------------------------------------------------------------

def analyze(events):
    """Count everything, from the normalized event list and nothing else."""
    lane = sorted((e for e in events
                   if e["kind"] in LANE_KINDS and e["t"] is not None),
                  key=lambda e: e["t"])
    timeline = sorted((e["t"], MARKERS[e["kind"]]) for e in events
                      if e["t"] is not None and e["kind"] in MARKERS)

    # Group into turns; wrapper "turns" that gather no activity are dropped.
    turns, cur = [], None
    for e in lane:
        if e["kind"] == "user":
            cur = {"prompt": e["label"], "promptChars": e["size"],
                   "start": e["t"], "wrapper": e.get("wrapper", False),
                   "events": [], "_tier": e["tier"]}
            turns.append(cur)
        elif cur is not None:
            ev = {k: e[k] for k in LANE_KEYS[e["kind"]]}
            ev["off"] = round((e["t"] - cur["start"]).total_seconds(), 1)
            if e.get("model"):
                cur.setdefault("models", []).append(e["model"])
            cur["events"].append(ev)
            cur["_tier"] = weakest(cur["_tier"], e["tier"])
    turns = [t for t in turns if not (t["wrapper"] and not t["events"])]

    prev_model = None
    for i, t in enumerate(turns):
        t["n"] = i + 1
        t["dur"] = max((e["off"] for e in t["events"]), default=0.0)
        t["startLabel"] = t["start"].strftime("%b %d, %H:%M UTC")
        models = t.pop("models", [])
        t["model"] = models[-1] if models else prev_model
        if prev_model and t["model"] and t["model"] != prev_model:
            t["note"] = "model → " + t["model"].replace("claude-", "")
        prev_model = t["model"] or prev_model
        t.pop("wrapper", None)
        tier = t.pop("_tier", TIER_FULL)

        asides = [e for e in t["events"] if e["kind"] == "aside"]
        reply = next((e for e in t["events"] if e["kind"] == "reply"), None)
        end = reply["off"] if reply else t["dur"]
        bounds = [0.0] + [a["off"] for a in asides] + [end]
        segs = []
        for j in range(len(bounds) - 1):
            a, b = bounds[j], bounds[j + 1]
            if b - a < 1:
                continue
            last = j == len(bounds) - 2
            ntools = sum(1 for e in t["events"] if e["kind"] == "tool"
                         and a <= e["off"] and (e["off"] <= b if last else e["off"] < b))
            if ntools == 0 and j == 0:
                continue  # silent lead-in before the first words
            opener = asides[j - 1]["label"] if 1 <= j <= len(asides) else None
            segs.append({"a": a, "b": b, "tools": ntools, "opener": opener,
                         "label": word_trim(opener) if opener else ""})
        t["segs"] = segs
        t["replyOff"] = reply["off"] if reply else None
        t["replySize"] = reply["size"] if reply else 0
        t["replyLabel"] = reply["label"] if reply else ""
        t["inflight"] = reply is None
        t["start"] = t["start"].isoformat()
        # Only below-full turns carry tier keys, so a full-tier report's
        # rendered payload is exactly what it always was.
        if tier != TIER_FULL:
            t["tier"] = tier
            if not TIER_HAS_DETAIL.get(tier, True):
                t["flat"] = True
                t["tierNote"] = "turn-level totals only"
            t["tierGap"] = TIER_GAP.get(tier, "")

    # ---- session-level metrics -------------------------------------------
    tot = {k: 0 for k in USAGE_KEYS}
    sidechain_out = 0
    thinking_tokens = 0
    for e in events:
        u = e.get("usage")
        if not isinstance(u, dict):
            continue
        for k in USAGE_KEYS:
            try:
                tot[k] += int(u.get(k, 0) or 0)
            except (TypeError, ValueError):
                pass
        det = u.get("output_tokens_details")
        if isinstance(det, dict):
            try:
                thinking_tokens += int(det.get("thinking_tokens", 0) or 0)
            except (TypeError, ValueError):
                pass
        if e.get("sidechain"):
            try:
                sidechain_out += int(u.get("output_tokens", 0) or 0)
            except (TypeError, ValueError):
                pass
    billed = sum(tot.values())
    input_total = billed - tot["output_tokens"]
    cache_pct = 100 * tot["cache_read_input_tokens"] / input_total if input_total else 0

    reasoning = execute = 0.0
    for (t1, k1), (t2, k2) in zip(timeline, timeline[1:]):
        d = (t2 - t1).total_seconds()
        if d > GAP_CAP_S:
            continue
        if k1 == "RESULT" and k2 == "MODEL":
            reasoning += d
        elif k1 == "MODEL" and k2 == "RESULT":
            execute += d

    prose = [e["size"] for t in turns for e in t["events"]
             if e["kind"] in ("aside", "reply")]
    replies = [e["size"] for t in turns for e in t["events"] if e["kind"] == "reply"]
    final_share = round(100 * sum(replies) / sum(prose)) if prose else 0

    silent_best, silent_turn = 0, None
    for t in turns:
        for is_tool, grp in itertools.groupby(t["events"], key=lambda e: e["kind"] == "tool"):
            if is_tool:
                run = len(list(grp))
                if run > silent_best:
                    silent_best, silent_turn = run, t["n"]

    text_reads = [(t["n"], e) for t in turns for e in t["events"]
                  if e["kind"] == "tool" and e["size"] and not e.get("is_image")]
    bigread = None
    if text_reads:
        n, e = max(text_reads, key=lambda x: x[1]["size"])
        bigread = {"turn": n, "off": e["off"], "size": e["size"],
                   "label": e["label"]}

    tool_counts = {}
    for t in turns:
        for e in t["events"]:
            if e["kind"] == "tool":
                tool_counts[e.get("tool") or "?"] = tool_counts.get(e.get("tool") or "?", 0) + 1

    all_ts = [x[0] for x in timeline]
    gaps = []
    for t1, t2 in zip(all_ts, all_ts[1:]):
        d = (t2 - t1).total_seconds()
        if d > COLD_GAP_MIN * 60:
            gaps.append({"from": t1.isoformat(), "hours": round(d / 3600, 1)})

    reasoning_events = [e for e in events if e["kind"] == "reasoning"]
    tier = TIER_FULL
    for t in turns:
        tier = weakest(tier, t.get("tier", TIER_FULL))

    you_chars = sum(t["promptChars"] for t in turns)
    metrics = {
        "turns": len(turns), "youChars": you_chars,
        "toolCalls": sum(tool_counts.values()), "toolCounts": tool_counts,
        "usage": tot, "billed": billed, "cachePct": round(cache_pct, 1),
        "sidechainOutputTokens": sidechain_out,
        "reasoningMin": round(reasoning / 60, 1), "executeMin": round(execute / 60, 1),
        "proseChars": sum(prose), "asides": sum(1 for t in turns for e in t["events"]
                                                if e["kind"] == "aside"),
        "finalBlockPct": final_share, "conversationChars": you_chars + sum(prose),
        "silentRun": silent_best, "silentTurn": silent_turn,
        "biggestTextRead": bigread, "gaps": gaps,
        "models": sorted({t["model"] for t in turns if t.get("model")}),
        "span": {"start": all_ts[0].isoformat() if all_ts else None,
                 "end": all_ts[-1].isoformat() if all_ts else None},
        # Extended-thinking blocks. Their text is frequently redacted to "" on
        # the wire, so trust the token count over the character count.
        "reasoningBlocks": len(reasoning_events),
        "reasoningChars": sum(e["size"] for e in reasoning_events),
        "thinkingTokens": thinking_tokens,
        "tier": tier, "tierGap": TIER_GAP.get(tier, ""),
        "hasDetail": TIER_HAS_DETAIL.get(tier, True),
    }
    return turns, metrics


# ---- rendering -----------------------------------------------------------

HOWTO_FULL = ('Each card is one of your prompts, on a shared 0&ndash;__MAXMIN__'
              '&nbsp;minute clock. The <b>pale bands</b> are phases of work, '
              'opening at the moment Claude said something out loud (the dot '
              '&mdash; hover it for the exact words). <b>Ticks</b> below are '
              'tool calls; taller&nbsp;=&nbsp;more text read back. The turn '
              'ends at the solid <b>reply</b> block.')


def stat(k, v, n):
    return (f'<div class="stat"><div class="k">{k}</div>'
            f'<div class="v">{v}</div><div class="n">{n}</div></div>')


def card(title, big, text):
    return (f'<div class="note"><h3>{title}</h3>'
            f'<p><span class="big">{big}</span><br>{text}</p></div>')


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def default_cards(m):
    cards = []
    if m["silentRun"]:
        cards.append(card("Longest silence", f'{m["silentRun"]} calls',
                          f'Consecutive tool calls in turn {m["silentTurn"]} with nothing '
                          f'said out loud. Long silent runs are where a user stops '
                          f'trusting that progress is happening.'))
    if m["executeMin"]:
        ratio = m["reasoningMin"] / m["executeMin"] if m["executeMin"] else 0
        cards.append(card("Where the time went",
                          f'{m["reasoningMin"]} : {m["executeMin"]}',
                          f'Minutes spent reasoning versus minutes running commands '
                          f'— roughly {ratio:.1f}× more thinking than executing.'))
    br = m.get("biggestTextRead")
    if br:
        cards.append(card("Biggest single read", f'{br["size"]:,}',
                          f'Characters returned by one tool call in turn {br["turn"]} '
                          f'({esc(br["label"])[:60]}) — against {m["conversationChars"]:,} '
                          f'characters of actual human-visible conversation.'))
    if m["finalBlockPct"]:
        cards.append(card("Saved for the end", f'{m["finalBlockPct"]}%',
                          f'Share of Claude’s words that arrived in the final reply of '
                          f'a turn; the other {100 - m["finalBlockPct"]}% were '
                          f'{m["asides"]} short asides while working.'))
    if m.get("tier", TIER_FULL) != TIER_FULL:
        gap = esc(m.get("tierGap") or "some detail was unavailable")
        tier = esc(m.get("tier") or "?")
        cards.append(card("What this source could not tell us", tier,
                          f'The session data reachable here is <b>{tier}</b>-tier: '
                          f'{gap}. Everything above is measured from it; nothing '
                          f'was inferred to fill the gap.'))
    return "\n".join(cards[:4])


def render(turns, metrics, template_path, labels=None):
    labels = labels or {}
    for t in turns:
        for s in t["segs"]:
            key = f'{t["n"]}:{round(s["a"])}'
            if key in labels.get("segments", {}):
                s["label"] = labels["segments"][key]

    maxdur = max((t["dur"] for t in turns), default=60)
    maxt = max(60, int((maxdur // 60 + 1) * 60))
    tok_m = metrics["billed"] / 1e6
    n_msg = metrics["turns"]
    m_word = "message" if n_msg == 1 else "messages"
    detail = metrics.get("hasDetail", True)
    is_full = metrics.get("tier", TIER_FULL) == TIER_FULL
    # A source that never exposed prompt text shouldn't be reported as "0 chars".
    prompts_known = is_full or metrics["youChars"] > 0

    # The placeholder already sits at the right indent, so only continuation
    # lines carry their own.
    legend = "\n      ".join([
        '<span class="lg"><i class="sw you"></i>You</span>',
        '<span class="lg"><i class="sw flag"></i>Claude narrates</span>',
        '<span class="lg"><i class="sw reply"></i>Reply</span>',
        '<span class="lg"><i class="sw tool"></i>Tool call</span>',
    ] if detail else [
        '<span class="lg"><i class="sw you"></i>Turn starts</span>',
        '<span class="lg"><i class="sw reply" style="background:var(--sunk);'
        'box-shadow:inset 0 0 0 1px var(--rule)"></i>Turn, no finer detail available'
        '</span>',
    ])

    stats_html = "\n".join([
        stat("You typed",
             f'{metrics["youChars"]:,}' if prompts_known else "&mdash;",
             f'characters, across {n_msg} {m_word}' if prompts_known
             else f'prompt text is not exposed by this source ({n_msg} {m_word})'),
        stat("Tokens billed", f'{tok_m:.1f}<small> M</small>' if tok_m >= 1
             else f'{metrics["billed"]:,}',
             f'{metrics["cachePct"]}% of input served from cache'),
        stat("Thinking vs. tools",
             (f'{metrics["reasoningMin"]/metrics["executeMin"]:.1f}&times;'
              if metrics["executeMin"] else "—"),
             f'{metrics["reasoningMin"]} min reasoning, {metrics["executeMin"]} min executing'),
        stat("Tool calls", f'{metrics["toolCalls"]}' if detail else "&mdash;",
             f'{metrics["silentRun"]} in the longest silent run' if detail
             else 'individual tool calls are not exposed by this source'),
    ])

    span = metrics["span"]
    date_label = (span["start"] or "")[:10]
    gaps_note = ""
    if metrics["gaps"]:
        g = max(metrics["gaps"], key=lambda x: x["hours"])
        gaps_note = (f' The session includes a {g["hours"]} h gap; wall-clock spans '
                     f'are per turn, not end to end.')
    if is_full:
        lede = (f'Every message, tool call and reply in one Claude Code session, placed '
                f'at the second it happened. {n_msg} human prompts — '
                f'<b>{metrics["youChars"]:,} characters</b> in all — set off '
                f'<b>{metrics["toolCalls"]} tool calls</b> and '
                f'<b>{tok_m:.1f}&nbsp;million billed tokens</b>.')
        footer = (f'Generated by <a href="https://github.com/konradcinkusz/claude-scope">'
                  f'claude-scope</a> from the session’s own JSONL transcript. Token counts '
                  f'come from the API usage fields and are exact; character counts are '
                  f'measured; tokens-from-characters figures are estimates. CLI '
                  f'bookkeeping records (local commands, model switches) are excluded '
                  f'from the lanes.{gaps_note}')
    else:
        gap = esc(metrics.get("tierGap") or "")
        tier = esc(metrics.get("tier") or "?")
        counted = (f'<b>{metrics["toolCalls"]} tool calls</b> and ' if detail else "")
        lede = (f'One coding-agent session, reconstructed from the session data this '
                f'agent could actually reach. {n_msg} {m_word}, {counted}'
                f'<b>{tok_m:.1f}&nbsp;million billed tokens</b>. Read from a '
                f'<b>{tier}</b>-tier source: {gap}.')
        footer = (f'Generated by <a href="https://github.com/konradcinkusz/claude-scope">'
                  f'claude-scope</a> from a <b>{tier}</b>-tier session source: {gap}. '
                  f'Token counts come from that source’s usage fields; character counts '
                  f'are measured. Everything shown is measured — nothing was inferred to '
                  f'fill the gaps.{gaps_note}')
    lede = labels.get("lede") or lede
    footer = labels.get("footer") or footer

    if not detail:
        howto = ('Each card is one turn, on a shared 0&ndash;__MAXMIN__&nbsp;minute '
                 'clock. This session’s source reports <b>turn-level totals only</b>, '
                 'so there are no phase bands and no per-tool ticks to draw — each '
                 'turn is a single block spanning the time it took. Nothing has been '
                 'invented to fill the space.')
    elif not prompts_known:
        howto = ('Each card is one <b>run of work</b>, on a shared 0&ndash;__MAXMIN__'
                 '&nbsp;minute clock — this source doesn’t record the prompts that '
                 'set each one off, so the cards are not questions. The <b>pale '
                 'bands</b> are phases of work, opening at the moment the assistant '
                 'said something out loud (the dot &mdash; hover it for the exact '
                 'words). <b>Ticks</b> below are tool calls; taller&nbsp;=&nbsp;more '
                 'text read back.')
    else:
        howto = HOWTO_FULL

    rows = sum(len(t["events"]) for t in turns) + len(turns)
    html = Path(template_path).read_text(encoding="utf-8")
    for k, v in {
        "__TITLE__": labels.get("title") or f'Session Report · {date_label}',
        "__EYEBROW__": labels.get("eyebrow") or
            f'claude-scope · {metrics["turns"]} turns · {date_label}',
        "__LEDE__": lede,
        "__STATS__": stats_html,
        "__HOWTO__": howto,
        "__LEGEND__": legend,
        "__CARDS__": labels.get("cards") or default_cards(metrics),
        "__FOOTER__": footer,
        "__ROWS__": str(rows),
        "__MAXT__": str(maxt),
        "__MAXMIN__": str(maxt // 60),
        "__TEXTCAP__": str(max((metrics.get("biggestTextRead") or {}).get("size", 0), 1000)),
        "__BIGREAD__": json.dumps(metrics.get("biggestTextRead")),
        "__DATA__": json.dumps(turns, separators=(",", ":")),
    }.items():
        html = html.replace(k, v)
    return html


def summary_text(turns, metrics):
    out = [f'session: {metrics["turns"]} turns, {metrics["toolCalls"]} tool calls, '
           f'{metrics["billed"]:,} tokens billed ({metrics["cachePct"]}% input from cache), '
           f'reasoning {metrics["reasoningMin"]} min vs tools {metrics["executeMin"]} min']
    if metrics.get("tier", TIER_FULL) != TIER_FULL:
        out.append(f'source tier: {metrics["tier"]} — {metrics.get("tierGap")}')
    for t in turns:
        out.append(f'\nTURN {t["n"]}  {t["startLabel"]}  dur {t["dur"]:.0f}s  '
                   f'{sum(1 for e in t["events"] if e["kind"] == "tool")} calls'
                   + ('  [in flight]' if t["inflight"] else ''))
        out.append(f'  prompt: {t["prompt"][:110]}')
        for s in t["segs"]:
            out.append(f'  seg {t["n"]}:{round(s["a"])}  {round(s["a"])}s–{round(s["b"])}s'
                       f'  {s["tools"]} calls  opener: {(s["opener"] or "(start)")[:90]}')
    return "\n".join(out)


def load_source(args):
    """(events, label for the report filename, path shown in the summary)."""
    if args.events:
        p = Path(args.events).expanduser()
        if not p.is_file():
            sys.exit(f"error: events file not found: {p}")
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            sys.exit(f"error: {p} is not valid JSON: {exc}")
        sid = (doc.get("session_id") if isinstance(doc, dict) else None) or p.stem
        return events_from_normalized(doc), str(sid)[:8], p
    if args.stream:
        p = Path(args.stream).expanduser()
        if not p.is_file():
            sys.exit(f"error: stream capture not found: {p}")
        recs = load_records(p)
        if not recs:
            sys.exit(f"error: no parseable records in {p}")
        sid = next((r.get("session_id") for r in recs if r.get("session_id")), p.stem)
        return events_from_stream(recs), str(sid)[:8], p
    p = locate_transcript(args.cwd, args.transcript)
    recs = load_records(p)
    if not recs:
        sys.exit(f"error: no parseable records in {p}")
    return events_from_jsonl(recs), p.stem[:8], p


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--transcript", help="explicit path to a session .jsonl")
    ap.add_argument("--stream", help="path to a stream-json capture "
                                     "(claude -p --output-format stream-json)")
    ap.add_argument("--events", help="path to a normalized event list this agent "
                                     "pre-fetched from its own session store "
                                     "(see references/session-sources.md)")
    ap.add_argument("--cwd", default=os.getcwd(),
                    help="project cwd used to locate the transcript (default: $PWD)")
    ap.add_argument("--out", default=".", help="output directory")
    ap.add_argument("--json", action="store_true", help="also write report JSON")
    ap.add_argument("--no-html", action="store_true", help="skip HTML rendering")
    ap.add_argument("--labels", help="JSON file with segment labels / lede / cards "
                                     "(see SKILL.md)")
    args = ap.parse_args()

    events, sid, path = load_source(args)
    if not events:
        sys.exit(f"error: no usable session events in {path}")
    turns, metrics = analyze(events)
    if not turns:
        sys.exit(f"error: no dialogue turns found in {path}")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    written = []
    if args.json:
        jp = outdir / f"session-report-{sid}.json"
        jp.write_text(json.dumps({"transcript": str(path), "turns": turns,
                                  "metrics": metrics}, indent=1), encoding="utf-8")
        written.append(jp)
    if not args.no_html:
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8")) \
            if args.labels else {}
        template = Path(__file__).resolve().parent.parent / "assets" / "template.html"
        hp = outdir / f"session-report-{sid}.html"
        hp.write_text(render(turns, metrics, template, labels), encoding="utf-8")
        written.append(hp)

    print(f"transcript: {path}")
    print(summary_text(turns, metrics))
    if written:
        print("\nwrote: " + ", ".join(str(w) for w in written))


if __name__ == "__main__":
    main()
