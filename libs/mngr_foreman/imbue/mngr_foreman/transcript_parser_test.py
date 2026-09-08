"""Tests for the vendored+modified Claude transcript parser."""

from __future__ import annotations

import json
from typing import Any

from imbue.mngr_foreman.transcript_parser import parse_claude_session_lines


def _line(**kwargs: Any) -> str:
    return json.dumps(kwargs)


def _assistant_tool_use(uuid: str, ts: str, tool_name: str, tool_input: dict, call_id: str = "call-1") -> str:
    return _line(
        type="assistant",
        uuid=uuid,
        timestamp=ts,
        message={
            "model": "claude",
            "content": [{"type": "tool_use", "id": call_id, "name": tool_name, "input": tool_input}],
        },
    )


def _tool_result(uuid: str, ts: str, call_id: str, output: str, is_error: bool = False) -> str:
    return _line(
        type="user",
        uuid=uuid,
        timestamp=ts,
        message={
            "content": [{"type": "tool_result", "tool_use_id": call_id, "content": output, "is_error": is_error}]
        },
    )


def test_user_and_assistant_text() -> None:
    lines = [
        _line(type="user", uuid="u1", timestamp="2026-01-01T00:00:00Z", message={"content": "hello there"}),
        _line(
            type="assistant",
            uuid="a1",
            timestamp="2026-01-01T00:00:01Z",
            message={"model": "claude", "content": [{"type": "text", "text": "hi back"}]},
        ),
    ]
    events = parse_claude_session_lines(lines)
    assert [e["type"] for e in events] == ["user_message", "assistant_message"]
    assert events[0]["content"] == "hello there"
    assert events[1]["text"] == "hi back"


def test_edit_attaches_full_untruncated_input_for_diff() -> None:
    old = "x" * 5000
    new = "y" * 5000
    line = _assistant_tool_use(
        "a1", "2026-01-01T00:00:00Z", "Edit", {"file_path": "/tmp/f.py", "old_string": old, "new_string": new}
    )
    events = parse_claude_session_lines([line], max_tool_output_chars=100)
    tc = events[0]["tool_calls"][0]
    # Diff tools must keep full strings regardless of the output cap.
    assert tc["input_full"]["old_string"] == old
    assert tc["input_full"]["new_string"] == new
    assert tc["input_full"]["file_path"] == "/tmp/f.py"


def test_write_and_multiedit_full_inputs() -> None:
    write_line = _assistant_tool_use("a1", "t1", "Write", {"file_path": "/a", "content": "line1\nline2"})
    multi_line = _assistant_tool_use(
        "a2",
        "t2",
        "MultiEdit",
        {
            "file_path": "/b",
            "edits": [{"old_string": "o1", "new_string": "n1"}, {"old_string": "o2", "new_string": "n2"}],
        },
    )
    events = parse_claude_session_lines([write_line, multi_line])
    assert events[0]["tool_calls"][0]["input_full"]["content"] == "line1\nline2"
    edits = events[1]["tool_calls"][0]["input_full"]["edits"]
    assert [e["new_string"] for e in edits] == ["n1", "n2"]


def test_non_diff_tool_input_is_capped_but_present() -> None:
    long_cmd = "echo " + "z" * 1000
    line = _assistant_tool_use("a1", "t1", "Bash", {"command": long_cmd})
    events = parse_claude_session_lines([line], max_tool_output_chars=50)
    full = events[0]["tool_calls"][0]["input_full"]["command"]
    assert full.startswith("echo z")
    assert full.endswith("...")
    assert len(full) <= 53


def test_tool_result_truncation_config() -> None:
    call = _assistant_tool_use("a1", "t1", "Bash", {"command": "ls"})
    result = _tool_result("u1", "t2", "call-1", "R" * 10000)
    events = parse_claude_session_lines([call, result], max_tool_output_chars=200)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert tr["output"].endswith("...")
    assert len(tr["output"]) == 203  # 200 + "..."
    assert tr["tool_name"] == "Bash"  # resolved via tool_name_by_call_id


def test_tool_result_unlimited_when_zero() -> None:
    call = _assistant_tool_use("a1", "t1", "Bash", {"command": "ls"})
    result = _tool_result("u1", "t2", "call-1", "R" * 5000)
    events = parse_claude_session_lines([call, result], max_tool_output_chars=0)
    tr = next(e for e in events if e["type"] == "tool_result")
    assert len(tr["output"]) == 5000


def test_meta_and_resume_markers_dropped() -> None:
    lines = [
        # resume continuation marker (isMeta user) -> dropped
        _line(
            type="user",
            uuid="u1",
            timestamp="t1",
            isMeta=True,
            message={"content": "Continue from where you left off."},
        ),
        # synthetic no-response reply -> dropped
        _line(
            type="assistant",
            uuid="a1",
            timestamp="t2",
            message={"model": "<synthetic>", "content": [{"type": "text", "text": "No response requested."}]},
        ),
        # a real message survives
        _line(type="user", uuid="u3", timestamp="t4", message={"content": "real"}),
    ]
    events = parse_claude_session_lines(lines)
    assert [e.get("content") for e in events] == ["real"]


def test_interrupt_and_wrappers_render_as_framework_chips() -> None:
    lines = [
        _line(type="user", uuid="i1", timestamp="t1", message={"content": "[Request interrupted by user]"}),
        _line(
            type="user", uuid="i2", timestamp="t2", message={"content": "[Request interrupted by user for tool use]"}
        ),
        _line(
            type="user",
            uuid="i3",
            timestamp="t3",
            message={"content": "<task_notification>agent X finished</task_notification>"},
        ),
        _line(
            type="user",
            uuid="i4",
            timestamp="t4",
            message={"content": "<system-reminder>be concise</system-reminder>"},
        ),
        _line(type="user", uuid="i5", timestamp="t5", message={"content": "here is 1. my real message"}),
    ]
    events = parse_claude_session_lines(lines)
    kinds = [(e["type"], e.get("label") or e.get("content")) for e in events]
    assert kinds == [
        ("framework_message", "interrupted"),
        ("framework_message", "interrupted"),
        ("framework_message", "task-notification"),
        ("framework_message", "system-reminder"),
        ("user_message", "here is 1. my real message"),  # a real message with an inline number is NOT chipped
    ]


def test_slash_command_invocation_is_framework() -> None:
    line = _line(
        type="user",
        uuid="u1",
        timestamp="t1",
        message={"content": "<command-name>login</command-name><command-args></command-args>"},
    )
    events = parse_claude_session_lines([line])
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "framework_message"
    assert ev["label"] == "/login"
    assert "content" not in ev  # a framework one-liner, not a user bubble


def test_slash_command_with_args_label() -> None:
    line = _line(
        type="user",
        uuid="u4",
        timestamp="t4",
        message={"content": "<command-name>model</command-name><command-args>opus</command-args>"},
    )
    events = parse_claude_session_lines([line])
    assert events[0]["type"] == "framework_message"
    assert events[0]["label"] == "/model opus"


def test_local_command_stdout_is_framework() -> None:
    line = _line(
        type="user",
        uuid="u2",
        timestamp="t2",
        message={"content": "<local-command-stdout>Login interrupted</local-command-stdout>"},
    )
    events = parse_claude_session_lines([line])
    assert len(events) == 1
    assert events[0]["type"] == "framework_message"
    assert events[0]["label"] == "Login interrupted"
    assert events[0]["detail"] == "Login interrupted"


def test_generic_meta_message_is_framework_not_dropped() -> None:
    # An isMeta message that is NOT the resume marker becomes a framework one-liner
    # (multi-line detail preserved; label is the clipped first line).
    line = _line(
        type="user",
        uuid="u3",
        timestamp="t3",
        isMeta=True,
        message={"content": "Caveat: messages below were generated while running local commands.\nsecond line"},
    )
    events = parse_claude_session_lines([line])
    assert len(events) == 1
    assert events[0]["type"] == "framework_message"
    assert events[0]["label"].startswith("Caveat:")
    assert "second line" in events[0]["detail"]


def test_local_command_caveat_wrapper_stripped() -> None:
    # The injected caveat renders as a framework line with the wrapper removed.
    text = "<local-command-caveat>Caveat: messages below were generated by local commands.</local-command-caveat>"
    line = _line(type="user", uuid="uc", timestamp="tc", isMeta=True, message={"content": text})
    events = parse_claude_session_lines([line])
    assert events[0]["type"] == "framework_message"
    assert events[0]["label"] == "Caveat: messages below were generated by local commands."
    assert "<local-command-caveat>" not in events[0]["detail"]


def test_normal_user_text_is_not_framework() -> None:
    line = _line(type="user", uuid="u5", timestamp="t5", message={"content": "please refactor the parser"})
    events = parse_claude_session_lines([line])
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "please refactor the parser"


def _image_result(uuid: str, ts: str, call_id: str, content: list) -> str:
    return _line(
        type="user",
        uuid=uuid,
        timestamp=ts,
        message={"content": [{"type": "tool_result", "tool_use_id": call_id, "content": content}]},
    )


def test_tool_result_image_passthrough() -> None:
    line = _image_result(
        "u1",
        "t1",
        "c1",
        [
            {"type": "text", "text": "here is the image"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        ],
    )
    events = parse_claude_session_lines([line])
    tr = [e for e in events if e["type"] == "tool_result"][0]
    assert tr["output"] == "here is the image"  # image not folded into text
    assert len(tr["images"]) == 1
    img = tr["images"][0]
    assert img["media_type"] == "image/png" and img["data"] == "AAAA"
    assert img["id"]  # a stable id for by-reference serving


def test_tool_result_image_only_has_empty_output() -> None:
    line = _image_result(
        "u2", "t2", "c2", [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "ZZ"}}]
    )
    tr = [e for e in parse_claude_session_lines([line]) if e["type"] == "tool_result"][0]
    assert tr["output"] == ""
    assert tr["images"][0]["media_type"] == "image/jpeg"
    assert tr["images"][0]["data"] == "ZZ"


def test_tool_result_non_base64_image_dropped() -> None:
    line = _image_result(
        "u3",
        "t3",
        "c3",
        [
            {"type": "image", "source": {"type": "url", "url": "http://x/y.png"}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": ""}},
        ],
    )
    tr = [e for e in parse_claude_session_lines([line]) if e["type"] == "tool_result"][0]
    assert "images" not in tr  # both invalid -> no images field


def test_tool_result_image_count_capped() -> None:
    imgs = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": str(i)}} for i in range(10)
    ]
    tr = [
        e for e in parse_claude_session_lines([_image_result("u4", "t4", "c4", imgs)]) if e["type"] == "tool_result"
    ][0]
    assert len(tr["images"]) == 6  # _MAX_TOOL_RESULT_IMAGES


def test_user_pasted_image_passthrough() -> None:
    # A human-pasted image: text + image blocks together in a user message.
    line = _line(
        type="user",
        uuid="up1",
        timestamp="t1",
        message={
            "content": [
                {"type": "text", "text": "look at this"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "PP"}},
            ]
        },
    )
    ev = [e for e in parse_claude_session_lines([line]) if e["type"] == "user_message"][0]
    assert ev["content"] == "look at this"
    assert ev["images"][0]["data"] == "PP"


def test_user_image_only_message_still_emitted() -> None:
    # An image-only paste (no text) must still produce a user_message.
    line = _line(
        type="user",
        uuid="up2",
        timestamp="t2",
        message={
            "content": [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "IO"}}]
        },
    )
    events = [e for e in parse_claude_session_lines([line]) if e["type"] == "user_message"]
    assert len(events) == 1
    assert events[0]["content"] == ""
    assert events[0]["images"][0]["data"] == "IO"


def test_queued_command_with_image() -> None:
    # A queued message whose prompt is a content list with a pasted image.
    line = _line(
        type="attachment",
        uuid="q1",
        timestamp="t3",
        attachment={
            "type": "queued_command",
            "commandMode": "prompt",
            "prompt": [
                {"type": "text", "text": "handle this"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QI"}},
            ],
        },
    )
    ev = [e for e in parse_claude_session_lines([line]) if e["type"] == "user_message"][0]
    assert ev["content"] == "handle this"
    assert ev["images"][0]["data"] == "QI"


def test_image_ids_are_distinct() -> None:
    line = _image_result(
        "u9",
        "t9",
        "c9",
        [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A"}},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "B"}},
        ],
    )
    tr = [e for e in parse_claude_session_lines([line]) if e["type"] == "tool_result"][0]
    ids = [img["id"] for img in tr["images"]]
    assert len(set(ids)) == 2  # distinct per-image ids


def test_lines_missing_uuid_or_timestamp_skipped() -> None:
    lines = [
        _line(type="user", timestamp="t1", message={"content": "no uuid"}),
        _line(type="user", uuid="u1", message={"content": "no ts"}),
        "not json at all",
        "",
    ]
    assert parse_claude_session_lines(lines) == []


def test_dedup_across_calls_with_shared_state() -> None:
    existing: set[str] = set()
    names: dict[str, str] = {}
    line = _line(type="user", uuid="u1", timestamp="t1", message={"content": "once"})
    first = parse_claude_session_lines([line], existing_event_ids=existing, tool_name_by_call_id=names)
    second = parse_claude_session_lines([line], existing_event_ids=existing, tool_name_by_call_id=names)
    assert len(first) == 1
    assert second == []  # already emitted -> deduped


def test_queued_command_attachment_parsed() -> None:
    line = _line(
        type="attachment",
        uuid="q1",
        timestamp="t1",
        attachment={"type": "queued_command", "commandMode": "prompt", "prompt": "queued msg"},
    )
    events = parse_claude_session_lines([line])
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "queued msg"
    # Flagged queued so the client styles it distinctly from a delivered turn.
    assert events[0]["queued"] is True


def test_delivered_user_message_is_not_flagged_queued() -> None:
    line = _line(type="user", uuid="u1", timestamp="t1", message={"role": "user", "content": "hello there"})
    events = parse_claude_session_lines([line])
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "hello there"
    assert "queued" not in events[0]  # delivered turns carry no queued flag


def test_slash_command_normalized() -> None:
    # A slash-command invocation is framework noise now, not a user bubble; the
    # rebuilt "/name args" text becomes its collapsed label (leading slash deduped).
    text = "<command-name>/deploy</command-name><command-args>prod</command-args>"
    line = _line(type="user", uuid="u1", timestamp="t1", message={"content": text})
    events = parse_claude_session_lines([line])
    assert events[0]["type"] == "framework_message"
    assert events[0]["label"] == "/deploy prod"


def _qop(operation: str, ts: str, content: str | None = None) -> str:
    d: dict[str, Any] = {"type": "queue-operation", "operation": operation, "timestamp": ts}
    if content is not None:
        d["content"] = content
    return json.dumps(d)


def _queue_view(events: list[dict[str, Any]]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for e in events:
        if e.get("type") == "user_message" and e.get("queued"):
            out.append(("Q", e["content"]))
        elif e.get("type") == "queue_accepted":
            out.append(("A", e["key"]))
        elif e.get("type") == "queue_removed":
            out.append(("R", e["key"]))
    return out


def test_queue_enqueue_emits_queued_user_message() -> None:
    # queue-operation records have NO uuid; they must still produce a queued bubble.
    events = parse_claude_session_lines([_qop("enqueue", "t1", "hi there")])
    assert _queue_view(events) == [("Q", "hi there")]
    assert events[0]["queued"] is True


def test_queue_remove_is_graceful_accept() -> None:
    events = parse_claude_session_lines([_qop("enqueue", "t1", "do X"), _qop("remove", "t2", "do X")])
    assert _queue_view(events) == [("Q", "do X"), ("A", "do X")]


def test_queue_dequeue_accepts_fifo_head_without_content() -> None:
    # Interrupt path: dequeue carries no content -> pop the FIFO head.
    lines = [
        _qop("enqueue", "t1", "first"),
        _qop("enqueue", "t2", "second"),
        _qop("dequeue", "t3"),
        _qop("dequeue", "t4"),
    ]
    assert _queue_view(parse_claude_session_lines(lines)) == [
        ("Q", "first"),
        ("Q", "second"),
        ("A", "first"),
        ("A", "second"),
    ]


def test_queue_popall_removes_all_pending() -> None:
    lines = [_qop("enqueue", "t1", "a"), _qop("enqueue", "t2", "b"), _qop("popAll", "t3", "a")]
    assert _queue_view(parse_claude_session_lines(lines)) == [("Q", "a"), ("Q", "b"), ("R", "a"), ("R", "b")]


def test_queue_replay_is_deterministic_and_dedups() -> None:
    # Persistent state across incremental calls converges; a re-read emits nothing new.
    lines = [_qop("enqueue", "t1", "m1"), _qop("remove", "t2", "m1")]
    eids: set[str] = set()
    queue: list[dict[str, Any]] = []
    first = parse_claude_session_lines(lines, existing_event_ids=eids, queue_state=queue)
    assert _queue_view(first) == [("Q", "m1"), ("A", "m1")]
    again = parse_claude_session_lines(lines, existing_event_ids=eids, queue_state=queue)
    assert _queue_view(again) == []  # already emitted -> deduped
    assert queue == []  # accepted -> no longer pending
