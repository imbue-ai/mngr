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
from imbue.minds.desktop_client.share_materials_injection import inject_share_grants_into_agent
from imbue.minds.desktop_client.share_materials_injection import inject_share_materials_into_agent
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
        relay_endpoint="relay-us1.infra.example:7000",
        relay_token="tok-123",
        connector_url="https://connector.example",
        broker_url="https://accounts.example",
    )
    assert f"export SHARE_WORKSPACE_DOMAIN={_DOMAIN}\n" in text
    assert "export SHARE_RELAY_ENDPOINT=relay-us1.infra.example:7000\n" in text
    assert "export SHARE_RELAY_TOKEN=tok-123\n" in text
    assert "export SHARE_CONNECTOR_URL=https://connector.example\n" in text
    assert "export SHARE_BROKER_URL=https://accounts.example\n" in text


def test_render_grants_toml_emits_valid_toml_with_quoted_entries() -> None:
    rendered = render_grants_toml(
        {"emails": ['weird"quote@example.com'], "email_domains": ["partner.org"]},
        {"my-app": {"emails": ["carol@example.com"], "email_domains": []}},
    )
    parsed = tomllib.loads(rendered)
    assert parsed["workspace"]["emails"] == ['weird"quote@example.com']
    assert parsed["workspace"]["email_domains"] == ["partner.org"]
    assert parsed["services"]["my-app"]["emails"] == ["carol@example.com"]


def test_inject_writes_files_via_base64_exec() -> None:
    caller = RecordingMngrCaller()
    agent_id = AgentId()

    inject_share_grants_into_agent(agent_id, "[workspace]\nemails = []\n", caller)
    inject_share_materials_into_agent(agent_id, "export SHARE_WORKSPACE_DOMAIN=x\n", caller)

    grants_call = " ".join(caller.calls[0])
    materials_call = " ".join(caller.calls[1])
    assert "share_grants.toml" in grants_call
    assert "share.env" in materials_call
    # The content rides base64-encoded so emails and tokens never need shell quoting.
    encoded = base64.b64encode(b"[workspace]\nemails = []\n").decode("ascii")
    assert encoded in grants_call


def test_inject_raises_on_exec_failure() -> None:
    caller = RecordingMngrCaller(result=MngrCallResult(returncode=1, stderr="boom"))

    with pytest.raises(ShareInjectionError):
        inject_share_grants_into_agent(AgentId(), "[workspace]\n", caller)


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
    inject_share_grants_into_agent(agent_id, grants_text, caller)

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


def test_writes_use_a_unique_tmp_name_per_write() -> None:
    # A fixed `<path>.tmp` shared by concurrent writers interleaves their
    # bytes; the write command must mint its tmp name via mktemp instead.
    caller = RecordingMngrCaller()

    inject_share_grants_into_agent(AgentId(), "[workspace]\n", caller)

    command = caller.calls[0][2]
    assert "mktemp data/.secrets/.share_grants.toml.XXXXXX" in command
    assert "share_grants.toml.tmp" not in command


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
                inject_share_grants_into_agent(agent_id, payload, caller)
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


def test_lock_registry_returns_one_lock_per_host() -> None:
    registry = MachineSharingLockRegistry()

    lock_a_first = registry.get_lock("host-" + "a" * 32)
    lock_a_second = registry.get_lock("host-" + "a" * 32)
    lock_b = registry.get_lock("host-" + "b" * 32)

    assert lock_a_first is lock_a_second
    assert lock_a_first is not lock_b
