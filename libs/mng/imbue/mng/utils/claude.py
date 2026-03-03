"""Shared utilities for querying Claude via the CLI.

Provides both streaming and non-streaming interfaces, all managed
through ConcurrencyGroup for proper subprocess lifecycle handling.
"""

import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.errors import ProcessSetupError
from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.imbue_common.pure import pure
from imbue.mng.errors import MngError

_CLAUDE_NOT_INSTALLED_MESSAGE: Final[str] = (
    "claude is not installed or not found in PATH. "
    "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code/overview"
)

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0


def _build_base_args(system_prompt: str) -> list[str]:
    return [
        "claude",
        "--print",
        "--system-prompt",
        system_prompt,
        "--no-session-persistence",
        "--tools",
        "",
    ]


@pure
def extract_text_delta(line: str) -> str | None:
    """Extract text from a stream-json content_block_delta event.

    Returns the delta text if the line is a content_block_delta with a text_delta,
    or None otherwise.
    """
    try:
        parsed = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None

    if parsed.get("type") != "stream_event":
        return None

    event = parsed.get("event")
    if not isinstance(event, dict):
        return None

    if event.get("type") != "content_block_delta":
        return None

    delta = event.get("delta")
    if not isinstance(delta, dict):
        return None

    if delta.get("type") != "text_delta":
        return None

    text = delta.get("text")
    if isinstance(text, str):
        return text

    return None


def query_claude(
    prompt: str,
    system_prompt: str,
    cg: ConcurrencyGroup,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> str | None:
    """Query Claude and return the complete response text, or None on failure.

    Uses ConcurrencyGroup for subprocess lifecycle management.
    Returns None if claude is not installed, times out, or returns an error.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="mng-claude-"))
    try:
        command = _build_base_args(system_prompt) + [prompt]
        result = cg.run_process_to_completion(
            command=command,
            timeout=timeout,
            cwd=tmp_dir,
            is_checked_after=False,
        )
        if result.returncode != 0:
            return None
        text = result.stdout.strip()
        return text if text else None
    except ProcessSetupError:
        return None
    except ProcessTimeoutError:
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def query_claude_streaming(
    prompt: str,
    system_prompt: str,
    cg: ConcurrencyGroup,
) -> Iterator[str]:
    """Query Claude and yield response text chunks as they arrive.

    Uses ConcurrencyGroup for subprocess lifecycle management.
    Raises MngError if claude is not installed or returns an error.
    """
    tmp_dir = Path(tempfile.mkdtemp(prefix="mng-claude-"))
    try:
        command = _build_base_args(system_prompt) + [
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            prompt,
        ]
        process = cg.run_process_in_background(
            command=command,
            cwd=tmp_dir,
        )

        is_error = False
        for line, is_stdout in process.stream_stdout_and_stderr():
            if not is_stdout:
                continue
            stripped = line.strip()
            if not stripped:
                continue

            try:
                parsed = json.loads(stripped)
                if parsed.get("type") == "result" and parsed.get("is_error"):
                    is_error = True
            except (json.JSONDecodeError, ValueError):
                pass

            text = extract_text_delta(stripped)
            if text is not None:
                yield text

        process.wait()

        if is_error or (process.poll() is not None and process.poll() != 0):
            stderr_content = process.read_stderr().strip()
            detail = stderr_content or "unknown error (no output captured)"
            raise MngError(f"claude failed (exit code {process.poll()}): {detail}")
    except ProcessSetupError:
        raise MngError(_CLAUDE_NOT_INSTALLED_MESSAGE) from None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
