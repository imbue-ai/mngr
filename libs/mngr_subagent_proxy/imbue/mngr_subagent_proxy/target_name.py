"""Shared target-name generation for spawned/denied subagents.

Both PROXY mode (``hooks/spawn.py``) and DENY mode (``hooks/deny.py``)
synthesize a per-Task-call mngr agent name of the shape
``<parent_name>--subagent-<slug>-<tid_suffix>`` so users can find them
with ``mngr list`` filters and so the ``mngr_subagent_proxy=child`` label
convention is honored consistently across modes.

Kept here so neither mode has to depend on the other.
"""

from __future__ import annotations


def slugify(text: str) -> str:
    """Lowercase, replace non-alnum with '-', collapse repeats, trim, cap at 30."""
    lowered = text.lower()
    converted_chars = [ch if ch.isalnum() else "-" for ch in lowered]
    converted = "".join(converted_chars)
    collapsed_parts: list[str] = []
    prev_dash = False
    for ch in converted:
        if ch == "-":
            if prev_dash:
                continue
            prev_dash = True
        else:
            prev_dash = False
        collapsed_parts.append(ch)
    collapsed = "".join(collapsed_parts).strip("-")
    capped = collapsed[:30]
    return capped.rstrip("-")


def build_subagent_target_name(parent_name: str, description: str, tool_use_id: str) -> str:
    """Build the mngr agent name for a Task call's spawned/denied subagent.

    Format: ``<parent_name>--subagent-<slug>-<tid_suffix>`` where
    ``<slug>`` is a slugified ``description`` (or the literal
    ``"subagent"`` when description slugifies to empty) and
    ``<tid_suffix>`` is the last 8 characters of ``tool_use_id``.

    Stable across PROXY and DENY modes so a child created by either
    path looks identical to ``mngr list``.
    """
    slug = slugify(description or "subagent") or "subagent"
    tid_suffix = tool_use_id[-8:]
    return f"{parent_name}--subagent-{slug}-{tid_suffix}"
