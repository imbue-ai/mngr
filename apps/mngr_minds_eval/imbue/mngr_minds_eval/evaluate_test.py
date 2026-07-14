"""Unit tests for the pure evaluation logic (no S3, no Anthropic API)."""

from __future__ import annotations

import json

from imbue.mngr_minds_eval import evaluate


def _transcript(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events)


def test_parse_transcript_keeps_real_turns_only() -> None:
    text = _transcript(
        {"type": "user_message", "content": "hi what can you do"},
        {"type": "assistant_message", "text": ""},  # internal placeholder, dropped
        {"type": "assistant_message", "text": "I can help with three things."},
        {"type": "user_message", "content": ""},  # empty, dropped
        {"type": "assistant_message", "text": "Pick a number one two or three"},
    )
    case = evaluate._parse_transcript(text)
    assert case.agent_turns == ["I can help with three things.", "Pick a number one two or three"]
    assert case.conversation == (
        "USER: hi what can you do\n\nAGENT: I can help with three things.\n\nAGENT: Pick a number one two or three"
    )


def test_avg_word_count_averages_agent_turns() -> None:
    case = evaluate._Case(agent_turns=["one two three", "four five"], conversation="")
    assert evaluate._eval_avg_word_count(case) == {"avg_word_count": 2.5}


def test_avg_word_count_empty_is_zero() -> None:
    case = evaluate._Case(agent_turns=[], conversation="")
    assert evaluate._eval_avg_word_count(case) == {"avg_word_count": 0.0}


def test_extract_json_tolerates_fences_and_prose() -> None:
    reply = 'Here you go:\n```json\n{"conciseness_score": 8, "proactive_score": 5}\n```\nthanks'
    assert evaluate._extract_json(reply) == {"conciseness_score": 8, "proactive_score": 5}


def test_aggregate_averages_numeric_keys() -> None:
    per_case = {
        "a": {"avg_word_count": 60.0, "conciseness_score": 8, "nontechnical_language_score": 7, "proactive_score": 6},
        "b": {"avg_word_count": 40.0, "conciseness_score": 6, "nontechnical_language_score": 9, "proactive_score": 4},
    }
    aggregate = evaluate._aggregate(per_case)
    assert aggregate == {
        "avg_word_count": 50.0,
        "conciseness_score": 7.0,
        "nontechnical_language_score": 8.0,
        "proactive_score": 5.0,
    }


def test_print_table_renders_na_for_unfinished(capsys) -> None:
    done = {"avg_word_count": 60.0, "conciseness_score": 8, "nontechnical_language_score": 7, "proactive_score": 6}
    evaluate._print_table([("done", done), ("pending", None)], done)
    out = capsys.readouterr().out
    assert "N/A" in out
    assert "done" in out and "pending" in out and "BATCH AVG" in out


def test_aggregate_skips_missing_keys() -> None:
    per_case = {"a": {"avg_word_count": 60.0}, "b": {"avg_word_count": None}}
    assert evaluate._aggregate(per_case)["avg_word_count"] == 60.0
    assert evaluate._aggregate(per_case)["conciseness_score"] is None
