#!/usr/bin/env python3
"""claude-scope session analyzer.

Parses a Claude Code session transcript (the JSONL the CLI writes under
~/.claude/projects/<munged-cwd>/<session-id>.jsonl), computes token / timing /
conversation-shape statistics deterministically, and renders a self-contained
HTML timeline from the bundled template.

Design rule: this script does ALL counting. The model reading its output only
labels phases and writes narrative. Never feed the raw transcript to the model
- a long session is hundreds of KB and the numbers must not depend on sampling.

The transcript format is undocumented and can change between CLI releases, so
parsing here is deliberately defensive: unknown record types and missing keys
are skipped, never fatal. See ../references/transcript-format.md for what each
handled shape means and which real sessions it was observed in.

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


def parse_ts(rec):
    t = rec.get("timestamp")
    if not t:
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
                 f"Pass one explicitly with --transcript PATH.")
    return candidates[0]


def load_records(path):
    recs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
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


def analyze(recs):
    results = {}
    for r in recs:
        if r.get("type") != "user":
            continue
        c = r.get("message", {}).get("content")
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = result_info(b)

    # One API response spans several assistant records sharing message.id;
    # count its usage once (first record wins) and each content block once.
    usage_by_msg, model_by_msg = {}, {}
    events, seen_blocks = [], set()
    timeline = []  # (ts, MODEL|RESULT|USER) for time attribution

    for r in recs:
        ts = parse_ts(r)
        side = bool(r.get("isSidechain"))
        rtype = r.get("type")
        msg = r.get("message") or {}
        if rtype == "assistant":
            mid = msg.get("id")
            if mid is not None and mid not in usage_by_msg:
                usage_by_msg[mid] = (msg.get("usage") or {}, side)
                model_by_msg[mid] = msg.get("model")
            if side:
                continue  # subagent traffic: billed (above) but not a lane event
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                key = (mid, b.get("type"), b.get("id") or (b.get("text") or "")[:50])
                if key in seen_blocks:
                    continue
                seen_blocks.add(key)
                if ts:
                    timeline.append((ts, "MODEL"))
                btype = b.get("type")
                if btype == "text" and (b.get("text") or "").strip():
                    txt = b["text"]
                    events.append({"t": ts, "kind":
                                   "reply" if len(txt) > ASIDE_MAX_CHARS else "aside",
                                   "label": " ".join(txt.split())[:160],
                                   "size": len(txt), "model": msg.get("model")})
                elif btype == "tool_use":
                    inp = b.get("input") or {}
                    size, is_img = results.get(b.get("id"), (0, False))
                    label = (inp.get("description") or inp.get("title")
                             or inp.get("query") or inp.get("prompt")
                             or b.get("name") or "tool")
                    events.append({"t": ts, "kind": "tool", "tool": b.get("name"),
                                   "label": str(label)[:160], "size": size,
                                   "is_image": is_img})
        elif rtype == "user" and not side:
            c = msg.get("content")
            if isinstance(c, str):
                if ts:
                    timeline.append((ts, "USER"))
                events.append({"t": ts, "kind": "user",
                               "label": " ".join(c.split()), "size": len(c),
                               "wrapper": c.startswith(WRAPPER_PREFIXES)})
            elif isinstance(c, list) and ts:
                timeline.append((ts, "RESULT"))

    events = [e for e in events if e["t"] is not None]
    events.sort(key=lambda e: e["t"])

    # Group into turns; wrapper "turns" that gather no activity are dropped.
    turns, cur = [], None
    for e in events:
        if e["kind"] == "user":
            cur = {"prompt": e["label"], "promptChars": e["size"],
                   "start": e["t"], "wrapper": e.get("wrapper", False),
                   "events": []}
            turns.append(cur)
        elif cur is not None:
            ev = {k: v for k, v in e.items() if k not in ("t", "model")}
            ev["off"] = round((e["t"] - cur["start"]).total_seconds(), 1)
            if e.get("model"):
                cur.setdefault("models", []).append(e["model"])
            cur["events"].append(ev)
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

    # ---- session-level metrics -------------------------------------------
    tot = {k: 0 for k in USAGE_KEYS}
    sidechain_out = 0
    for u, side in usage_by_msg.values():
        for k in USAGE_KEYS:
            tot[k] += u.get(k, 0) or 0
        if side:
            sidechain_out += u.get("output_tokens", 0) or 0
    billed = sum(tot.values())
    input_total = billed - tot["output_tokens"]
    cache_pct = 100 * tot["cache_read_input_tokens"] / input_total if input_total else 0

    timeline.sort()
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
    }
    return turns, metrics


# ---- rendering -----------------------------------------------------------

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

    stats_html = "\n".join([
        stat("You typed", f'{metrics["youChars"]:,}',
             f'characters, across {n_msg} {m_word}'),
        stat("Tokens billed", f'{tok_m:.1f}<small> M</small>' if tok_m >= 1
             else f'{metrics["billed"]:,}',
             f'{metrics["cachePct"]}% of input served from cache'),
        stat("Thinking vs. tools",
             (f'{metrics["reasoningMin"]/metrics["executeMin"]:.1f}&times;'
              if metrics["executeMin"] else "—"),
             f'{metrics["reasoningMin"]} min reasoning, {metrics["executeMin"]} min executing'),
        stat("Tool calls", f'{metrics["toolCalls"]}',
             f'{metrics["silentRun"]} in the longest silent run'),
    ])

    span = metrics["span"]
    date_label = (span["start"] or "")[:10]
    gaps_note = ""
    if metrics["gaps"]:
        g = max(metrics["gaps"], key=lambda x: x["hours"])
        gaps_note = (f' The session includes a {g["hours"]} h gap; wall-clock spans '
                     f'are per turn, not end to end.')
    lede = labels.get("lede") or (
        f'Every message, tool call and reply in one Claude Code session, placed at the '
        f'second it happened. {n_msg} human prompts — <b>{metrics["youChars"]:,} '
        f'characters</b> in all — set off <b>{metrics["toolCalls"]} tool calls</b> and '
        f'<b>{tok_m:.1f}&nbsp;million billed tokens</b>.')
    footer = labels.get("footer") or (
        f'Generated by <a href="https://github.com/konradcinkusz/claude-scope">claude-scope</a> '
        f'from the session’s own JSONL transcript. Token counts come from the API usage '
        f'fields and are exact; character counts are measured; tokens-from-characters figures '
        f'are estimates. CLI bookkeeping records (local commands, model switches) are excluded '
        f'from the lanes.{gaps_note}')

    rows = sum(len(t["events"]) for t in turns) + len(turns)
    html = Path(template_path).read_text(encoding="utf-8")
    for k, v in {
        "__TITLE__": labels.get("title") or f'Session Report · {date_label}',
        "__EYEBROW__": labels.get("eyebrow") or
            f'claude-scope · {metrics["turns"]} turns · {date_label}',
        "__LEDE__": lede,
        "__STATS__": stats_html,
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
    for t in turns:
        out.append(f'\nTURN {t["n"]}  {t["startLabel"]}  dur {t["dur"]:.0f}s  '
                   f'{sum(1 for e in t["events"] if e["kind"] == "tool")} calls'
                   + ('  [in flight]' if t["inflight"] else ''))
        out.append(f'  prompt: {t["prompt"][:110]}')
        for s in t["segs"]:
            out.append(f'  seg {t["n"]}:{round(s["a"])}  {round(s["a"])}s–{round(s["b"])}s'
                       f'  {s["tools"]} calls  opener: {(s["opener"] or "(start)")[:90]}')
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--transcript", help="explicit path to a session .jsonl")
    ap.add_argument("--cwd", default=os.getcwd(),
                    help="project cwd used to locate the transcript (default: $PWD)")
    ap.add_argument("--out", default=".", help="output directory")
    ap.add_argument("--json", action="store_true", help="also write report JSON")
    ap.add_argument("--no-html", action="store_true", help="skip HTML rendering")
    ap.add_argument("--labels", help="JSON file with segment labels / lede / cards "
                                     "(see SKILL.md)")
    args = ap.parse_args()

    path = locate_transcript(args.cwd, args.transcript)
    recs = load_records(path)
    if not recs:
        sys.exit(f"error: no parseable records in {path}")
    turns, metrics = analyze(recs)
    if not turns:
        sys.exit(f"error: no dialogue turns found in {path}")

    sid = path.stem[:8]
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
