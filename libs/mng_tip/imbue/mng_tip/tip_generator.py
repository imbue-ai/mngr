"""Background tip generator for the mng-tip plugin.

Reads recent invocation history, queries Claude for a usage tip, and writes
the result to next_tip.txt. Designed to be run as a detached subprocess via
``python -m imbue.mng_tip.tip_generator``.
"""

import json
from datetime import datetime
from datetime import timezone
from typing import Final

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mng.utils.claude import query_claude
from imbue.mng_tip.invocation_logger import get_tip_data_dir
from imbue.mng_tip.invocation_logger import read_recent_invocations

_SYSTEM_PROMPT: Final[str] = """\
You are a tips assistant for `mng`, a CLI tool for managing AI coding agents \
across local, Docker, and Modal hosts.

Based on the user's recent command history, suggest a useful mng feature they \
don't seem to know about or under-utilize.

Each tip should ideally be under 80 characters and certainly under 200 \
characters. Be specific and include an example command when possible.

You may suggest things you've suggested before for variety, but try not to \
repeat the most recent ones.

Here are features you might suggest:
- mng pair <agent>: continuous bidirectional file sync
- mng clone <agent> <new-name>: duplicate an existing agent
- mng push/pull <agent>: one-shot code sync between local and agent
- mng message --all -m "task": send a message to all running agents
- mng create --no-connect -m "task": fire-and-forget background task
- mng create -n 5 --in modal: spin up multiple agents at once
- mng list --watch 5: real-time agent monitoring (refresh every 5s)
- mng list --format json: machine-readable output
- mng create --template <name>: use a saved create template
- mng create --add-command "cmd": extra tmux window alongside agent
- mng create --env-file .env: pass secrets to agent environment
- mng create --known-host github.com: enable SSH access to hosts
- mng snapshot <agent>: save agent state for later restoration
- mng ask "question": get CLI help from AI
- mng create -- --model opus: pass arguments to underlying agent
- mng list --include 'expr': filter agents with CEL expressions
- mng create --host <host>: share a host across multiple agents

Respond with ONLY the tip text. No quotes, no prefix, no markdown.\
"""

_PROCESS_TIMEOUT_SECONDS: Final[float] = 60.0


def _read_previous_suggestions(max_lines: int = 20) -> list[str]:
    """Read previous tip suggestions to avoid consecutive repeats."""
    suggestions_path = get_tip_data_dir() / "suggestions.jsonl"
    if not suggestions_path.exists():
        return []

    lines = suggestions_path.read_text().strip().splitlines()
    recent = lines[-max_lines:]

    suggestions: list[str] = []
    for line in recent:
        try:
            record = json.loads(line)
            suggestions.append(record.get("suggestion", ""))
        except (json.JSONDecodeError, ValueError):
            continue
    return suggestions


def _save_suggestion(tip_text: str) -> None:
    """Save a tip to both next_tip.txt and the suggestions log."""
    tip_dir = get_tip_data_dir()
    tip_dir.mkdir(parents=True, exist_ok=True)

    (tip_dir / "next_tip.txt").write_text(tip_text)

    suggestions_path = tip_dir / "suggestions.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "suggestion": tip_text,
    }
    with open(suggestions_path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def generate_tip() -> str | None:
    """Generate a tip based on recent invocation history.

    Returns the tip text, or None if generation fails or there is
    insufficient history.
    """
    invocations = read_recent_invocations(max_lines=200)
    if not invocations:
        return None

    # Build the invocation summary (compact: just command and argv)
    invocation_lines: list[str] = []
    for record in invocations:
        cmd = record.get("command", "?")
        argv = record.get("argv", [])
        invocation_lines.append(f"{cmd}: {' '.join(argv)}")
    invocation_text = "\n".join(invocation_lines)

    # Build prompt
    prompt_parts = [f"Recent invocations:\n{invocation_text}"]

    previous = _read_previous_suggestions()
    if previous:
        prompt_parts.append(f"Previous suggestions:\n" + "\n".join(previous[-10:]))

    prompt = "\n\n".join(prompt_parts)

    with ConcurrencyGroup(name="mng-tip-generator") as cg:
        return query_claude(
            prompt=prompt,
            system_prompt=_SYSTEM_PROMPT,
            cg=cg,
            timeout=_PROCESS_TIMEOUT_SECONDS,
        )


def main() -> None:
    """Entry point for background tip generation."""
    # Skip if next_tip.txt already exists (another generation in progress or already done)
    next_tip_path = get_tip_data_dir() / "next_tip.txt"
    if next_tip_path.exists():
        return

    tip = generate_tip()
    if tip is not None:
        _save_suggestion(tip)


if __name__ == "__main__":
    main()
