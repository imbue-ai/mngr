import base64
import json
import subprocess
import threading
import tomllib
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import Field

from imbue.minds.desktop_client.share_materials_injection import MachineSharingLockRegistry
from imbue.minds.desktop_client.share_materials_injection import ShareInjectionError
from imbue.minds.desktop_client.share_materials_injection import build_share_env_text
from imbue.minds.desktop_client.share_materials_injection import clear_share_materials_from_agent
from imbue.minds.desktop_client.share_materials_injection import probe_share_state_in_agent
from imbue.minds.desktop_client.share_materials_injection import provision_share_files_in_agent
from imbue.minds.desktop_client.share_materials_injection import read_share_grants_from_agent
from imbue.minds.desktop_client.share_materials_injection import render_grants_toml
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


class _ExecutingMngrCaller(MngrCaller):
    """Caller that actually runs the exec'd shell command in a local directory.

    ``mngr exec`` argv is ``["exec", <agent>, <command>, ...flags]``; the
    command (argv[2]) is run under bash with the given directory as the
    workspace root, so the real write/read shell behavior -- mktemp, atomic
    mv, `|| true` -- is exercised without any mngr or container.

    Faithful to the CLI's stdout contract, which is what makes reads through
    this double meaningful: human format appends mngr's own ``Command
    succeeded on agent <name>`` status line to stdout (the pollution that once
    made every raw grants read parse as malformed), and ``--format json``
    wraps the command's output in the result envelope.
    """

    work_dir: Path = Field(frozen=True, description="Directory standing in for the workspace root")

    def call(
        self,
        argv: Sequence[str],
        timeout: float | None = None,
        env_overrides: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> MngrCallResult:
        completed = subprocess.run(
            ["bash", "-c", argv[2]],
            cwd=self.work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        is_json_format = "json" in argv and "--format" in argv
        if is_json_format:
            envelope = {
                "results": [
                    {
                        "agent": argv[1],
                        "stdout": completed.stdout,
                        "stderr": completed.stderr,
                        "success": completed.returncode == 0,
                    }
                ]
            }
            return MngrCallResult(returncode=completed.returncode, stdout=json.dumps(envelope), stderr="")
        trailer = f"Command succeeded on agent {argv[1]}\n" if completed.returncode == 0 else ""
        return MngrCallResult(
            returncode=completed.returncode, stdout=completed.stdout + trailer, stderr=completed.stderr
        )


_DOMAIN = "host-" + "a" * 32 + "." + "b" * 32 + ".us1.shares.example"


def test_build_share_env_text_matches_the_gateway_contract() -> None:
    text = build_share_env_text(
        workspace_domain=_DOMAIN,
        relay_token="tok-123",
        connector_url="https://connector.example",
        broker_url="https://accounts.example",
        chrome_origin="https://connector.example",
    )
    assert f"export SHARE_WORKSPACE_DOMAIN={_DOMAIN}\n" in text
    assert "SHARE_RELAY_ENDPOINT" not in text
    assert "export SHARE_RELAY_TOKEN=tok-123\n" in text
    assert "export SHARE_CONNECTOR_URL=https://connector.example\n" in text
    assert "export SHARE_BROKER_URL=https://accounts.example\n" in text
    assert "export SHARE_CHROME_ORIGIN=https://connector.example\n" in text


def test_build_share_env_text_omits_the_chrome_origin_line_when_empty() -> None:
    text = build_share_env_text(
        workspace_domain=_DOMAIN,
        relay_token="tok-123",
        connector_url="https://connector.example",
        broker_url="https://accounts.example",
        chrome_origin="",
    )
    assert "SHARE_CHROME_ORIGIN" not in text


def test_render_grants_toml_emits_valid_toml_with_quoted_entries() -> None:
    rendered = render_grants_toml(
        {"emails": ['weird"quote@example.com'], "email_domains": ["partner.org"]},
        {"my-app": {"emails": ["carol@example.com"], "email_domains": []}},
    )
    parsed = tomllib.loads(rendered)
    assert parsed["workspace"]["emails"] == ['weird"quote@example.com']
    assert parsed["workspace"]["email_domains"] == ["partner.org"]
    assert parsed["services"]["my-app"]["emails"] == ["carol@example.com"]


def test_provision_writes_all_share_files_in_one_exec() -> None:
    caller = RecordingMngrCaller()
    agent_id = AgentId()
    grants_text = "[workspace]\nemails = []\n"
    env_text = "export SHARE_WORKSPACE_DOMAIN=x\n"

    provision_share_files_in_agent(agent_id, grants_text, "owner@example.com", env_text, caller)

    # One exec carries everything -- this is the whole point (each exec pays a
    # full mngr process + SSH round trip on remote hosts).
    assert len(caller.calls) == 1
    command = caller.calls[0][2]
    assert "share_grants.toml" in command
    assert "data/.state/share/owner_email" in command
    assert "data/.secrets/share.env" in command
    # The contents ride base64-encoded so emails and tokens never need shell quoting.
    assert base64.b64encode(grants_text.encode()).decode("ascii") in command
    assert base64.b64encode(b"owner@example.com").decode("ascii") in command
    assert base64.b64encode(env_text.encode()).decode("ascii") in command
    # share.env is written LAST: the gateway brings the stack up the moment it
    # appears, so the grants must already be in place by then.
    assert command.index("share_grants.toml") < command.index("data/.secrets/share.env")
    assert command.index("owner_email") < command.index("data/.secrets/share.env")
    # The best-effort owner clause is brace-grouped with its `|| true` INSIDE
    # the group: && / || are left-associative, so a bare `... || true && ...`
    # would swallow a grants failure and publish share.env without grants.
    assert "{ mkdir -p data/.state/share" in command
    assert "|| true; }" in command
    assert not command.endswith("|| true")


def test_provision_omits_share_env_on_a_grants_only_update() -> None:
    caller = RecordingMngrCaller()

    provision_share_files_in_agent(AgentId(), "[workspace]\n", "owner@example.com", None, caller)

    command = caller.calls[0][2]
    assert "share_grants.toml" in command
    assert "share.env" not in command


def test_provision_skips_an_empty_owner_email() -> None:
    caller = RecordingMngrCaller()

    provision_share_files_in_agent(AgentId(), "[workspace]\n", "", "export A=b\n", caller)

    command = caller.calls[0][2]
    assert "owner_email" not in command
    assert "share_grants.toml" in command
    assert "share.env" in command


def test_provision_raises_on_exec_failure() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="boom"))

    with pytest.raises(ShareInjectionError):
        provision_share_files_in_agent(AgentId(), "[workspace]\n", "owner@example.com", "export A=b\n", caller)


def test_provision_owner_email_failure_never_fails_the_share_writes(tmp_path: Path) -> None:
    # The owner-email file is a convenience artifact; its clause is wrapped so
    # a failure (here: its parent path exists as a FILE, so mkdir -p fails)
    # cannot fail the exec or stop share.env from landing.
    caller = _ExecutingMngrCaller(work_dir=tmp_path)
    (tmp_path / "data" / ".state").mkdir(parents=True)
    (tmp_path / "data" / ".state" / "share").write_text("not a directory")

    provision_share_files_in_agent(AgentId(), "[workspace]\n", "owner@example.com", "export A=b\n", caller)

    assert (tmp_path / "data" / ".secrets" / "share_grants.toml").read_text() == "[workspace]\n"
    assert (tmp_path / "data" / ".secrets" / "share.env").read_text() == "export A=b\n"


def test_provision_grants_failure_stops_share_env_from_landing(tmp_path: Path) -> None:
    # A failed grants write must surface as an error with share.env unpublished.
    # (The brace-grouping of the owner clause -- asserted structurally in the
    # one-exec test above -- is what keeps its `|| true` from swallowing a
    # grants failure, since shell && / || are left-associative.)
    caller = _ExecutingMngrCaller(work_dir=tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".secrets").write_text("not a directory")

    with pytest.raises(ShareInjectionError):
        provision_share_files_in_agent(AgentId(), "[workspace]\n", "owner@example.com", "export A=b\n", caller)

    assert not (tmp_path / "data" / ".secrets" / "share.env").exists()


def test_read_share_grants_returns_none_when_absent(tmp_path: Path) -> None:
    # `|| true` folds the absent-file case into rc 0 with empty command stdout
    # inside the envelope -- and mngr's own status line must not read as content.
    caller = _ExecutingMngrCaller(work_dir=tmp_path)

    assert read_share_grants_from_agent(AgentId(), caller) is None


def test_read_share_grants_round_trips_the_document_through_the_exec_envelope(tmp_path: Path) -> None:
    # The read rides --format json precisely because human-format mngr exec
    # appends "Command succeeded on agent <name>" to stdout; a raw read parsed
    # that trailer as part of the document and reported valid grants malformed.
    grants_text = render_grants_toml({"emails": ["a@example.com"], "email_domains": []}, {})
    caller = _ExecutingMngrCaller(work_dir=tmp_path)
    agent_id = AgentId()
    provision_share_files_in_agent(agent_id, grants_text, "owner@example.com", None, caller)

    assert read_share_grants_from_agent(agent_id, caller) == grants_text


def test_read_share_grants_requests_the_json_envelope() -> None:
    grants_text = "[workspace]\nemails = []\n"
    recording = RecordingMngrCaller(
        result=MngrCallResult(
            returncode=0,
            stdout=json.dumps({"results": [{"agent": "a", "stdout": grants_text, "stderr": "", "success": True}]}),
        )
    )

    assert read_share_grants_from_agent(AgentId(), recording) == grants_text
    assert recording.calls[0][-2:] == ["--format", "json"]


def test_read_share_grants_raises_on_exec_failure() -> None:
    # A failed exec must stay distinguishable from an absent document, or a
    # caller could mistake an unreadable policy for an empty one.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="offline"))

    with pytest.raises(ShareInjectionError):
        read_share_grants_from_agent(AgentId(), caller)


def test_read_share_grants_raises_on_an_unrecognized_exec_envelope() -> None:
    # rc 0 with stdout that is not the JSON envelope (e.g. an mngr that
    # ignored --format) is "the read never landed", never file content.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout="[workspace]\nnot an envelope"))

    with pytest.raises(ShareInjectionError):
        read_share_grants_from_agent(AgentId(), caller)


def test_clear_share_materials_is_best_effort_and_no_start() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="offline"))

    clear_share_materials_from_agent(AgentId(), caller)

    joined = " ".join(caller.calls[0])
    assert "rm -f" in joined
    assert "--no-start" in joined
    # The owner-email file is removed alongside the secrets at unshare.
    assert "data/.state/share/owner_email" in joined


def test_writes_use_a_unique_tmp_name_per_write() -> None:
    # A fixed `<path>.tmp` shared by concurrent writers interleaves their
    # bytes; every write clause must mint its tmp name via mktemp instead.
    caller = RecordingMngrCaller()

    provision_share_files_in_agent(AgentId(), "[workspace]\n", "owner@example.com", "export A=b\n", caller)

    command = caller.calls[0][2]
    assert "mktemp data/.secrets/.share_grants.toml.XXXXXX" in command
    assert "mktemp data/.state/share/.owner_email.XXXXXX" in command
    assert "mktemp data/.secrets/.share.env.XXXXXX" in command
    assert ".tmp" not in command


def test_concurrent_grant_writes_never_corrupt_the_file(tmp_path: Path) -> None:
    caller = _ExecutingMngrCaller(work_dir=tmp_path)
    agent_id = AgentId()
    payload_a = render_grants_toml({"emails": ["a@example.com"], "email_domains": []}, {})
    payload_b = render_grants_toml({"emails": ["b@example.com"], "email_domains": ["partner.org"]}, {})

    # Release both writers together on every round to maximize overlap.
    barrier = threading.Barrier(2)
    write_errors: list[Exception] = []

    def _write_repeatedly(payload: str) -> None:
        for _ in range(10):
            try:
                barrier.wait(timeout=30)
                provision_share_files_in_agent(agent_id, payload, "owner@example.com", None, caller)
            except (ShareInjectionError, threading.BrokenBarrierError) as exc:
                write_errors.append(exc)
                return

    threads = [threading.Thread(target=_write_repeatedly, args=(payload,)) for payload in (payload_a, payload_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert write_errors == []
    final_text = (tmp_path / "data" / ".secrets" / "share_grants.toml").read_text()
    # The surviving document is always one writer's payload in full -- a valid
    # parse alone would not catch a last-writer-wins mix of the two.
    assert final_text in (payload_a, payload_b)
    tomllib.loads(final_text)


def test_probe_reports_everything_absent_in_an_empty_workspace(tmp_path: Path) -> None:
    caller = _ExecutingMngrCaller(work_dir=tmp_path)

    probe = probe_share_state_in_agent(AgentId(), caller)

    assert probe.has_gateway is False
    assert probe.has_share_env is False
    assert probe.grants_toml_text is None


def test_probe_round_trips_the_full_share_state_in_one_exec(tmp_path: Path) -> None:
    caller = _ExecutingMngrCaller(work_dir=tmp_path)
    (tmp_path / "system" / "services" / "share_gateway").mkdir(parents=True)
    grants_text = render_grants_toml({"emails": ["a@example.com"], "email_domains": []}, {})
    provision_share_files_in_agent(AgentId(), grants_text, "owner@example.com", "export A=b\n", caller)

    probe = probe_share_state_in_agent(AgentId(), caller)

    assert probe.has_gateway is True
    assert probe.has_share_env is True
    assert probe.grants_toml_text == grants_text


def test_probe_treats_an_empty_grants_file_as_absent(tmp_path: Path) -> None:
    # A present-but-empty document grants nobody, so the probe reports it as
    # None just like an absent one. The checked read keeps this safe: a failed
    # read reports UNREADABLE (and raises), so an empty value can only mean a
    # genuinely empty file.
    caller = _ExecutingMngrCaller(work_dir=tmp_path)
    (tmp_path / "system" / "services" / "share_gateway").mkdir(parents=True)
    secrets_dir = tmp_path / "data" / ".secrets"
    secrets_dir.mkdir(parents=True)
    (secrets_dir / "share.env").write_text("export A=b\n")
    (secrets_dir / "share_grants.toml").write_text("")

    probe = probe_share_state_in_agent(AgentId(), caller)

    assert probe.has_gateway is True
    assert probe.has_share_env is True
    assert probe.grants_toml_text is None


def test_probe_is_conservative_on_exec_failure() -> None:
    # A failed probe must refuse (everything absent) rather than provision a
    # share against unknown state; nothing has been written or created yet.
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="offline"))

    probe = probe_share_state_in_agent(AgentId(), caller)

    assert probe.has_gateway is False
    assert probe.has_share_env is False
    assert probe.grants_toml_text is None
    assert caller.calls[0][-3:] == ["--no-start", "--format", "json"]


def test_probe_raises_on_an_undecodable_grants_payload() -> None:
    # An unreadable existing policy must never be mistaken for an absent one:
    # the caller would otherwise overwrite grants nobody ever saw.
    envelope = json.dumps(
        {
            "results": [
                {
                    "agent": "a",
                    "stdout": "MNGR_SHARE_GATEWAY=1\nMNGR_SHARE_ENV=1\nMNGR_SHARE_GRANTS_B64=@@not-base64@@\n",
                    "stderr": "",
                    "success": True,
                }
            ]
        }
    )
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=envelope))

    with pytest.raises(ShareInjectionError):
        probe_share_state_in_agent(AgentId(), caller)


def test_probe_raises_when_the_grants_file_exists_but_cannot_be_read() -> None:
    # The script's checked read reports UNREADABLE (rather than an empty value)
    # when the document exists but the read fails; the parser must raise, or
    # the caller's data-loss guard would treat the unreadable policy as absent
    # and silently overwrite it.
    envelope = json.dumps(
        {
            "results": [
                {
                    "agent": "a",
                    "stdout": "MNGR_SHARE_GATEWAY=1\nMNGR_SHARE_ENV=1\nMNGR_SHARE_GRANTS_B64=UNREADABLE\n",
                    "stderr": "",
                    "success": True,
                }
            ]
        }
    )
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=0, stdout=envelope))

    with pytest.raises(ShareInjectionError):
        probe_share_state_in_agent(AgentId(), caller)


def test_lock_registry_returns_one_lock_per_host() -> None:
    registry = MachineSharingLockRegistry()

    lock_a_first = registry.get_lock("host-" + "a" * 32)
    lock_a_second = registry.get_lock("host-" + "a" * 32)
    lock_b = registry.get_lock("host-" + "b" * 32)

    assert lock_a_first is lock_a_second
    assert lock_a_first is not lock_b
