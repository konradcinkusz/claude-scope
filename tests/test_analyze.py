#!/usr/bin/env python3
"""Tests for the session analyzer. Stdlib only, like the script itself.

    python3 -m unittest discover -s tests -v      (or just: python3 tests/test_analyze.py)

Fixtures under tests/fixtures/ are synthetic — never a real transcript — but
each line exists to reproduce a trap actually observed on a live session and
written up in references/transcript-format.md / references/session-sources.md.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "plugins" / "claude-scope" / "skills" / "session-report"
sys.path.insert(0, str(SKILL / "scripts"))

import analyze as A  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
TEMPLATE = SKILL / "assets" / "template.html"


def jsonl_events():
    return A.events_from_jsonl(A.load_records(FIX / "session.jsonl"))


class NormalizedModel(unittest.TestCase):
    """The boundary analyze() actually consumes."""

    def test_every_adapter_emits_only_known_kinds(self):
        for events in (jsonl_events(),
                       A.events_from_stream(A.load_records(FIX / "stream.jsonl")),
                       A.events_from_normalized(
                           json.loads((FIX / "partial-events.json").read_text()))):
            self.assertTrue(events)
            for e in events:
                self.assertIn(e["kind"], A.KNOWN_KINDS)
                self.assertIn(e["tier"], A.TIER_RANK)
                self.assertTrue(set(e) >= {"t", "kind", "size", "label", "tool",
                                           "is_image", "model", "usage", "tier"})

    def test_analyze_needs_nothing_but_the_event_list(self):
        turns, metrics = A.analyze([
            A.event(A.parse_iso("2026-01-01T00:00:00Z"), "user", size=5, label="hi"),
            A.event(A.parse_iso("2026-01-01T00:00:10Z"), "reply", size=600,
                    label="there", usage={"output_tokens": 7}),
        ])
        self.assertEqual(len(turns), 1)
        self.assertEqual(metrics["billed"], 7)


class ThinkingBlocks(unittest.TestCase):
    """Step 4: extended reasoning used to be dropped on the floor."""

    def test_thinking_becomes_a_reasoning_event(self):
        kinds = [e["kind"] for e in jsonl_events()]
        self.assertIn("reasoning", kinds)

    def test_reasoning_is_counted_in_metrics(self):
        _, m = A.analyze(jsonl_events())
        self.assertEqual(m["reasoningBlocks"], 1)
        self.assertEqual(m["reasoningChars"], len("weighing two options"))
        # the honest token figure, which survives redaction of the text
        self.assertEqual(m["thinkingTokens"], 7)

    def test_stream_adapter_also_surfaces_reasoning(self):
        _, m = A.analyze(A.events_from_stream(A.load_records(FIX / "stream.jsonl")))
        self.assertEqual(m["reasoningBlocks"], 1)
        # observed live: `thinking` is frequently redacted to "" on the wire,
        # so a zero character count is correct, not a parsing failure
        self.assertEqual(m["reasoningChars"], 0)
        self.assertEqual(m["thinkingTokens"], 16)

    def test_reasoning_text_never_reaches_a_report(self):
        secret = "weighing two options"
        turns, metrics = A.analyze(jsonl_events())
        html = A.render(turns, metrics, TEMPLATE)
        self.assertNotIn(secret, html)
        self.assertNotIn(secret, json.dumps(turns))
        self.assertNotIn(secret, A.summary_text(turns, metrics))

    def test_reasoning_does_not_enter_the_lanes(self):
        """It must not split a silent run or add a row to the event table."""
        turns, _ = A.analyze(jsonl_events())
        for t in turns:
            for e in t["events"]:
                self.assertIn(e["kind"], ("aside", "reply", "tool"))


class JsonlPathUnchanged(unittest.TestCase):
    """The JSONL path's rendered output is the contract; guard its shape."""

    def setUp(self):
        self.turns, self.metrics = A.analyze(jsonl_events())

    def test_known_counts(self):
        m = self.metrics
        self.assertEqual(m["turns"], 1)            # the /model pseudo-turn is dropped
        self.assertEqual(m["toolCalls"], 2)
        self.assertEqual(m["tier"], A.TIER_FULL)
        # usage counted once per message id, subagent included
        self.assertEqual(m["billed"], 1060 + 1025 + 10 + 532)
        self.assertEqual(m["sidechainOutputTokens"], 9)

    def test_subagent_prose_is_not_a_lane_event(self):
        self.assertNotIn("subagent chatter", json.dumps(self.turns))

    def test_image_result_does_not_win_biggest_text_read(self):
        self.assertEqual(self.metrics["biggestTextRead"]["size"], 400)

    def test_lane_events_carry_exactly_the_pinned_keys_in_order(self):
        """Adding fields to the normalized model must never change a report."""
        for t in self.turns:
            for e in t["events"]:
                expected = A.LANE_KEYS[e["kind"]] + ("off",)
                self.assertEqual(tuple(e.keys()), expected)

    def test_full_tier_turns_carry_no_tier_keys(self):
        """Absence is what keeps a full-tier payload byte-identical."""
        for t in self.turns:
            for key in ("tier", "flat", "tierNote", "tierGap"):
                self.assertNotIn(key, t)

    def test_renders(self):
        html = A.render(self.turns, self.metrics, TEMPLATE)
        for placeholder in ("__DATA__", "__HOWTO__", "__LEGEND__", "__STATS__"):
            self.assertNotIn(placeholder, html)


class StreamAdapter(unittest.TestCase):
    """Verified live: two traps that would silently corrupt the numbers."""

    def setUp(self):
        self.events = A.events_from_stream(A.load_records(FIX / "stream.jsonl"))
        self.turns, self.metrics = A.analyze(self.events)

    def test_usage_comes_from_the_result_record_not_summed_per_record(self):
        # summing the mid-stream partials would give output_tokens 5, not 135
        self.assertEqual(self.metrics["usage"]["output_tokens"], 135)
        self.assertEqual(self.metrics["billed"], 4 + 50 + 800 + 135)

    def test_a_turn_exists_even_though_prompts_are_never_emitted(self):
        self.assertEqual(self.metrics["turns"], 1)
        self.assertEqual(self.turns[0]["promptChars"], 0)
        self.assertIn("not recorded", self.turns[0]["prompt"])

    def test_tier_is_stream_and_keeps_intra_turn_detail(self):
        self.assertEqual(self.metrics["tier"], A.TIER_STREAM)
        self.assertTrue(self.metrics["hasDetail"])
        self.assertEqual(self.metrics["toolCalls"], 1)
        self.assertNotIn("flat", self.turns[0])

    def test_report_states_what_the_source_could_not_give(self):
        html = A.render(self.turns, self.metrics, TEMPLATE)
        self.assertIn("stream", html)
        self.assertIn("prompt text is not exposed by this source", html)


class NormalizedPartialSource(unittest.TestCase):
    """A turn-level-only source must degrade honestly, not invent a timeline."""

    def setUp(self):
        doc = json.loads((FIX / "partial-events.json").read_text())
        self.turns, self.metrics = A.analyze(A.events_from_normalized(doc))

    def test_neutral_usage_names_are_accepted(self):
        u = self.metrics["usage"]
        self.assertEqual(u["input_tokens"], 310)
        self.assertEqual(u["output_tokens"], 1240)
        self.assertEqual(u["cache_read_input_tokens"], 5900)
        self.assertEqual(u["cache_creation_input_tokens"], 100)

    def test_junk_entries_are_skipped_not_fatal(self):
        self.assertEqual(self.metrics["turns"], 2)

    def test_no_fabricated_segments_or_tool_calls(self):
        for t in self.turns:
            self.assertEqual(t["segs"], [])
        self.assertEqual(self.metrics["toolCalls"], 0)

    def test_turns_are_marked_flat_and_explain_themselves(self):
        for t in self.turns:
            self.assertTrue(t["flat"])
            self.assertEqual(t["tier"], A.TIER_PARTIAL)
            self.assertTrue(t["tierGap"])

    def test_render_explains_rather_than_showing_empty_lanes(self):
        html = A.render(self.turns, self.metrics, TEMPLATE)
        self.assertIn("turn-level totals only", html)
        self.assertIn("individual tool calls are not exposed by this source", html)
        self.assertIn("Nothing has been invented to fill the space", html)
        # the phase-band legend would be a lie here
        self.assertNotIn("Claude narrates", html)


class DefensiveParsing(unittest.TestCase):
    """Unknown shapes are skipped, never fatal — for every adapter."""

    def test_garbage_never_raises(self):
        junk = [{}, {"type": "assistant"}, {"type": "user", "message": None},
                {"type": "assistant", "message": {"content": ["not a dict", 7]}},
                {"type": "result"}, {"type": "who-knows", "timestamp": "nope"}]
        for adapter in (A.events_from_jsonl, A.events_from_stream):
            self.assertIsInstance(adapter(junk), list)
        for doc in ({}, [], {"events": "not a list"}, {"events": [None, 3]},
                    {"events": [{"kind": "user", "size": "NaN", "t": "bad"}]}):
            self.assertIsInstance(A.events_from_normalized(doc), list)

    def test_half_written_trailing_line_is_tolerated(self):
        self.assertTrue(A.load_records(FIX / "session.jsonl"))

    def test_unknown_tier_falls_back_instead_of_crashing(self):
        events = A.events_from_normalized(
            {"capability_tier": "invented", "events":
             [{"kind": "user", "t": "2026-01-01T00:00:00Z", "size": 3}]})
        self.assertEqual(events[0]["tier"], A.TIER_PARTIAL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
