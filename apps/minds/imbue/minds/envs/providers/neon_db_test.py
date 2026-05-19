"""Unit tests for the Neon DB provider's lookup + adoption helpers.

The HTTP layer goes through ``httpx`` and is exercised end-to-end by
``test_provisioning_*``; here we cover the pure decision helper that
turns a list of matching Neon projects into "adopt", "create", or
"refuse loudly with cleanup recipe" -- the F32 fix.
"""

import pytest

from imbue.minds.envs.providers.neon_db import NeonProjectSummary
from imbue.minds.envs.providers.neon_db import NeonProviderError
from imbue.minds.envs.providers.neon_db import _format_multi_match_message
from imbue.minds.envs.providers.neon_db import _select_one_or_raise_multi_match


def _make_summary(*, id: str, created_at: str = "2026-01-01T00:00:00Z") -> NeonProjectSummary:
    return NeonProjectSummary(id=id, name="minds-dev-josh", created_at=created_at)


def test_select_one_or_raise_returns_none_when_no_projects_match() -> None:
    assert _select_one_or_raise_multi_match([], "minds-dev-josh", org_id="org-x") is None


def test_select_one_or_raise_returns_the_unique_match() -> None:
    only = _make_summary(id="p1")
    assert _select_one_or_raise_multi_match([only], "minds-dev-josh", org_id="org-x") is only


def test_select_one_or_raise_refuses_loud_on_multi_match_with_all_ids() -> None:
    projects = [
        _make_summary(id="cool-scene-88886167", created_at="2026-05-17T16:17:46Z"),
        _make_summary(id="wispy-dream-81207052", created_at="2026-05-17T20:33:52Z"),
        _make_summary(id="late-butterfly-16683624", created_at="2026-05-17T23:53:42Z"),
    ]
    with pytest.raises(NeonProviderError) as exc_info:
        _select_one_or_raise_multi_match(projects, "minds-dev-josh", org_id="org-jolly-cell-77900540")
    message = str(exc_info.value)
    # All three IDs must be in the message so the operator can act
    assert "cool-scene-88886167" in message
    assert "wispy-dream-81207052" in message
    assert "late-butterfly-16683624" in message
    # The org id (from the input) must be in the message
    assert "org-jolly-cell-77900540" in message
    # The project name (from the input) must be in the message
    assert "minds-dev-josh" in message
    # And the cleanup recipe must be present so the operator can copy-paste
    assert "curl" in message
    assert "DELETE" in message
    assert "Authorization: Bearer $NEON_TOKEN" in message
    # And the per-project line must include creation time for triage
    assert "2026-05-17T16:17:46Z" in message
    assert "2026-05-17T20:33:52Z" in message
    assert "2026-05-17T23:53:42Z" in message


def test_format_multi_match_sorts_oldest_first_in_the_per_project_list() -> None:
    # Insertion order shuffled deliberately to confirm the formatter sorts
    projects = [
        _make_summary(id="middle", created_at="2026-05-17T20:00:00Z"),
        _make_summary(id="newest", created_at="2026-05-17T23:00:00Z"),
        _make_summary(id="oldest", created_at="2026-05-17T16:00:00Z"),
    ]
    message = _format_multi_match_message(projects, project_name="minds-dev-josh", org_id="org-x")

    oldest_idx = message.index("id=oldest")
    middle_idx = message.index("id=middle")
    newest_idx = message.index("id=newest")
    assert oldest_idx < middle_idx < newest_idx, (
        "Per-project list should be sorted oldest-first so the most-recent (usually the live) project is shown last."
    )

    # The "nuke every project" one-liner must list every project id in
    # the same oldest-first order so a copy-paste yields a deterministic
    # `for PID in ...` loop.
    one_liner_idx = message.index("for PID in ")
    nuke_segment = message[one_liner_idx : message.index(";", one_liner_idx)]
    assert nuke_segment.index("oldest") < nuke_segment.index("middle") < nuke_segment.index("newest")
