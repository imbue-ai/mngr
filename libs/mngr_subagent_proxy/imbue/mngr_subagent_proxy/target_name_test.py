"""Unit tests for the shared target-name generator."""

from __future__ import annotations

from imbue.mngr_subagent_proxy.target_name import build_subagent_target_name
from imbue.mngr_subagent_proxy.target_name import slugify


def test_slugify_lowercases_and_replaces_non_alphanumerics() -> None:
    assert slugify("Hello, World!") == "hello-world"


def test_slugify_collapses_dash_runs() -> None:
    assert slugify("A B  C") == "a-b-c"


def test_slugify_strips_edges_and_caps_length() -> None:
    assert slugify("----") == ""
    assert slugify("a" * 50) == "a" * 30


def test_build_subagent_target_name_canonical_form() -> None:
    """Format pinned: ``<parent>--subagent-<slug>-<tid_suffix>`` with tid_suffix = last 8 chars."""
    name = build_subagent_target_name("parent-agent", "Code Review!", "toolu_xyz98765432")
    assert name == "parent-agent--subagent-code-review-98765432"


def test_build_subagent_target_name_falls_back_to_literal_subagent_for_empty_description() -> None:
    """Empty / unsluggable description still produces a deterministic name.

    Without this, two Task calls with empty descriptions in the same
    session would produce identical-looking targets up to the tid
    suffix, making `mngr list` output harder to read.
    """
    name = build_subagent_target_name("parent", "", "toolu_aaaa12345678")
    assert name == "parent--subagent-subagent-12345678"
    name = build_subagent_target_name("parent", "----", "toolu_bbbb12345678")
    assert name == "parent--subagent-subagent-12345678"


def test_build_subagent_target_name_uses_last_8_chars_of_tool_use_id() -> None:
    """tid_suffix is fixed at 8 chars regardless of tool_use_id length."""
    name = build_subagent_target_name("p", "x", "toolu_short")
    assert name.endswith("-lu_short")
    name = build_subagent_target_name("p", "x", "toolu_aaaaaaaaaaaaaaaaaaaaaaa")
    assert name.endswith("-aaaaaaaa")
