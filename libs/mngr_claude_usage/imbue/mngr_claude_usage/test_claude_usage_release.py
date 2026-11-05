from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from typing import Final

import pytest
from pydantic import ValidationError

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import agent_state_dir
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_claude.testing import AGENT_DESTROY_TIMEOUT_SECONDS
from imbue.mngr_claude.testing import ClaudeReleaseProfile
from imbue.mngr_claude.testing import build_payload_saving_command
from imbue.mngr_claude.testing import commit_project_claude_settings
from imbue.mngr_claude.testing import created_agent
from imbue.mngr_claude.testing import drift_note
from imbue.mngr_claude.testing import read_saved_payloads
from imbue.mngr_claude.testing import wait_for_turn_to_end
from imbue.mngr_usage.data_types import CostMode
from imbue.mngr_usage.data_types import CostSnapshot

_PAYLOAD_TIMEOUT_SECONDS: Final[float] = 90.0
_USAGE_TIMEOUT_SECONDS: Final[float] = 120.0
_USAGE_COMMAND_TIMEOUT_SECONDS: Final[float] = 120.0
_EVENTS_TAIL_LINES: Final[int] = 5
_TURN_PROMPT: Final[str] = "Reply with just OK."
_CLAUDE_USAGE_SOURCE: Final[str] = "claude"
_USER_STATUSLINE_CMD_RELPATH: Final[str] = "commands/user_statusline_cmd"
_USAGE_EVENTS_RELPATH: Final[str] = "events/claude/usage/events.jsonl"
_WRITER_CONSUMER: Final[str] = "claude_usage_writer.sh's session_id and cost reads"
_READER_CONSUMERS: Final[str] = (
    "mngr_usage.api._parse_usage_event, _cost_from_event, _walk_agent_events and _classify_process, "
    "and mngr_usage.cli._write_source_section for the human line"
)


def _total_cost_usd(payload: Mapping[str, Any]) -> float:
    cost = payload.get("cost")
    value = cost.get("total_cost_usd") if isinstance(cost, dict) else None
    return float(value) if isinstance(value, (int, float)) else 0.0


def _run_usage(
    profile: ClaudeReleaseProfile, ctx: AgentReleaseContext, agent_name: str, *extra_args: str
) -> subprocess.CompletedProcess[str]:
    return profile.run_mngr(
        ctx,
        "usage",
        "--no-preserved",
        "--include",
        f'name == "{agent_name}"',
        *extra_args,
        timeout=_USAGE_COMMAND_TIMEOUT_SECONDS,
    )


def _events_tail(ctx: AgentReleaseContext) -> str:
    events_path = agent_state_dir(ctx.host_dir) / _USAGE_EVENTS_RELPATH
    if not events_path.exists():
        return f"({_USAGE_EVENTS_RELPATH} does not exist)"
    return "\n".join(events_path.read_text().splitlines()[-_EVENTS_TAIL_LINES:])


@pytest.mark.release
@pytest.mark.harness_internals(harness="claude")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_statusline_cost_reaches_mngr_usage_as_api_spend(tmp_path: Path) -> None:
    """``mngr usage`` reports an API-key agent's turn as API spend equal to the cost claude gave its statusline.

    Pins the statusline stdin fields claude_usage_writer.sh reads: ``session_id``, ``cost`` and ``rate_limits``.
    """
    profile = ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    dump_dir = tmp_path / "statusline-payloads"
    dump_dir.mkdir()
    statusline_command = f"{build_payload_saving_command(dump_dir)}; echo mngr-usage-probe"
    commit_project_claude_settings(
        ctx.project_dir,
        {"statusLine": {"type": "command", "command": statusline_command}},
        "Add a statusLine that saves its stdin",
    )
    agent_name = f"claude-usage-{get_short_random_string()}"
    drift = drift_note(ctx, "the statusline payload")
    with created_agent(profile, ctx, agent_name, destroy_timeout=AGENT_DESTROY_TIMEOUT_SECONDS):
        state_dir = agent_state_dir(ctx.host_dir)
        assert (state_dir / _USER_STATUSLINE_CMD_RELPATH).read_text() == statusline_command, (
            "mngr did not capture the project's statusLine command as the shim's chained user command; "
            "consumer: mngr_claude_usage.plugin._capture_existing_statusline_command"
        )

        send_expecting_success(profile, ctx, agent_name, _TURN_PROMPT)
        wait_for_turn_to_end(profile, ctx, _TURN_PROMPT, drift)
        session_id = (state_dir / "claude_session_id").read_text().strip()

        def session_payloads() -> list[dict[str, Any]]:
            return [payload for payload in read_saved_payloads(dump_dir) if payload.get("session_id") == session_id]

        assert poll_until(
            lambda: any(_total_cost_usd(payload) > 0 for payload in session_payloads()),
            timeout=_PAYLOAD_TIMEOUT_SECONDS,
            poll_interval=2.0,
        ), (
            f"no statusline payload carries the agent's session_id {session_id} and a positive cost.total_cost_usd "
            f"{drift}; session_ids seen: {sorted({str(p.get('session_id')) for p in read_saved_payloads(dump_dir)})}; "
            f"keys seen: {sorted({k for p in read_saved_payloads(dump_dir) for k in p})}; consumer: {_WRITER_CONSUMER}"
        )
        for payload in session_payloads():
            try:
                CostSnapshot.model_validate(payload.get("cost"))
            except ValidationError as error:
                pytest.fail(
                    f"the statusline payload's cost no longer validates as CostSnapshot {drift}: {error}; "
                    "consumer: mngr_usage.api._cost_from_event, which drops the whole cost block with only a debug "
                    "log, so mngr usage shows no spend"
                )
        assert not any(payload.get("rate_limits") for payload in session_payloads()), (
            f"the statusline payload carries rate_limits under an ANTHROPIC_API_KEY login {drift}; "
            "consumer: mngr_usage.api._classify_process, which then files real API spend as SUBSCRIPTION (imputed)"
        )

        last_view: dict[str, Any] = {}

        def usage_reports_the_session_cost() -> bool:
            expected = max(_total_cost_usd(payload) for payload in session_payloads())
            result = _run_usage(profile, ctx, agent_name, "--format", "json", "--detail")
            assert result.returncode == 0, f"mngr usage failed:\n{result.stdout}\n{result.stderr}"
            view = json.loads(result.stdout.strip().splitlines()[-1])
            last_view["view"] = view
            last_view["expected"] = expected
            for source in view.get("sources", []):
                if source.get("source") != _CLAUDE_USAGE_SOURCE:
                    continue
                records = [s for s in source.get("sessions", []) if s.get("session_id") == session_id]
                last_view["source"] = source
                return (
                    len(records) == 1
                    and records[0]["cost"]["total_cost_usd"] == pytest.approx(expected)
                    and source["api_cost"]["total_cost_usd"] == pytest.approx(expected)
                )
            return False

        assert poll_until(usage_reports_the_session_cost, timeout=_USAGE_TIMEOUT_SECONDS, poll_interval=5.0), (
            f"mngr usage does not report session {session_id} once, at the statusline's cost.total_cost_usd of "
            f"{last_view.get('expected')}, although the raw statusline checks passed {drift}; "
            f"last view: {last_view.get('view')}; usage events tail:\n{_events_tail(ctx)}\n"
            f"consumers: {_WRITER_CONSUMER}, {_READER_CONSUMERS}, "
            "and claude honouring the shim's settings.local.json statusLine over the project's"
        )
        source = last_view["source"]
        session = next(s for s in source["sessions"] if s["session_id"] == session_id)
        assert (
            session["cost_mode"] == CostMode.API_KEY
            and source["subscription_session_count"] == 0
            and source["api_session_count"] >= 1
        ), (
            f"mngr usage did not report the API-key session as API spend: {source} {drift}; consumers: {_READER_CONSUMERS}"
        )
        human = _run_usage(profile, ctx, agent_name)
        expected_line = f"api cost: ${last_view['expected']:.2f}"
        assert human.returncode == 0 and expected_line in human.stdout and "subscription cost" not in human.stdout, (
            f"mngr usage's human output lacks {expected_line!r} or shows a subscription cost line {drift}; "
            f"consumers: {_READER_CONSUMERS}:\n{human.stdout}\n{human.stderr}"
        )
