"""``minds_services`` test: real-LLM-call-through-litellm via a local Docker DEFAULT_WORKSPACE_TEMPLATE workspace.

The "but does this actually work" test for imbue_cloud LLM key minting
+ litellm proxy routing + spend tracking, exercised through the same
product path a user takes since AI credentials moved out of the create
flow: the workspace boots unauthenticated, a LiteLLM key is minted for
the signed-in user (the same connector mint the desktop app's
``/settings/ai-keys`` page drives, with the same workspace-keyed alias),
and the resulting env-var credential blob is submitted through the
workspace's own ``/api/claude-auth/submit-credentials`` endpoint -- the
strict endpoint behind the sign-in modal's paste textarea, which writes
the shared Claude settings env block and restarts the workspace's
claude agents. A real chat message then proves the agent serves traffic
through the minted key, and the litellm token row proves the spend was
tracked.

The mint-page and modal *browser UI* legs are covered elsewhere (the
ai_keys unit tests and the Electron modal sign-in test in
test_snapshot_resume.py); this test owns the cross-service integration:
connector mint -> workspace credential write -> litellm-proxied claude
traffic -> spend row.

Runs locally against the operator's Docker daemon (skips when Docker or
the orchestrator-prepared template worktree is unavailable). When this
moves to offload, the future ``offload-modal-minds-services.toml`` will
enable Docker-in-Docker (mirroring ``offload-modal-acceptance.toml``).
"""

import json
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path

import psycopg2
import pytest
import tomlkit
from loguru import logger

from imbue.minds.deployment_tests.data_types import DefaultWorkspaceTemplateRef
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.data_types import VerifiedUserHandle
from imbue.minds.deployment_tests.helpers import signin_and_mint_litellm_key
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.desktop_client.ai_keys import build_credential_blob
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.release, pytest.mark.minds_services]

_CREATE_TIMEOUT_SECONDS = 1200
_IN_CONTAINER_TIMEOUT_SECONDS = 120
_SYSTEM_INTERFACE_READY_ATTEMPTS = 60
_CHAT_REPLY_ATTEMPTS = 60
_SPEND_POLL_ATTEMPTS = 30


def _run(command: list[str], *, cwd: Path | None = None, timeout: int) -> subprocess.CompletedProcess[str]:
    logger.info("Running: {}", " ".join(command))
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def _exec_in_container(container_name: str, command: str, *, timeout: int) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "exec", container_name, "bash", "-lc", command], timeout=timeout)


def _prepare_template_clone(source_worktree: Path) -> Path:
    """Clone the orchestrator-prepared template checkout into a scratch dir.

    The clone gets ``is_allowed_in_pytest = true`` appended to its mngr
    settings (mngr's config guard refuses to run under PYTEST_CURRENT_TEST
    otherwise); the orchestrator's own worktree is never mutated.
    """
    clone_target = Path(tempfile.mkdtemp(prefix="litellm-e2e-dwt-")) / "default-workspace-template"
    clone = _run(
        ["git", "clone", "--local", f"file://{source_worktree}", str(clone_target)],
        timeout=600,
    )
    assert clone.returncode == 0, f"template clone failed: {clone.stderr}"
    settings_path = clone_target / ".mngr" / "settings.toml"
    doc = tomlkit.parse(settings_path.read_text())
    doc["is_allowed_in_pytest"] = True
    settings_path.write_text(tomlkit.dumps(doc))
    return clone_target


def _create_docker_workspace(template_path: Path, host_name: str) -> tuple[str, str]:
    """Run the real ``mngr create`` the desktop client runs; return (agent_id, host_id)."""
    create = _run(
        [
            "mngr",
            "create",
            f"system-services@{host_name}.docker",
            "--new-host",
            "--no-connect",
            "--label",
            "is_primary=true",
            "--template",
            "main",
            "--template",
            "docker",
            "--format",
            "jsonl",
        ],
        cwd=template_path,
        timeout=_CREATE_TIMEOUT_SECONDS,
    )
    assert create.returncode == 0, f"mngr create failed: {create.stderr[-2000:]}"
    agent_id, host_id = "", ""
    for line in create.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "created":
            agent_id = str(event.get("agent_id", ""))
            host_id = str(event.get("host_id", ""))
    assert agent_id and host_id, f"mngr create emitted no created event: {create.stdout[-2000:]}"
    return agent_id, host_id


def _find_container_name(host_id: str) -> str:
    listing = _run(
        ["docker", "ps", "--filter", f"label=com.imbue.mngr.host-id={host_id}", "--format", "{{.Names}}"],
        timeout=60,
    )
    names = [name for name in listing.stdout.splitlines() if name.strip()]
    assert names, f"No docker container carries mngr host id {host_id}"
    return names[0]


def _wait_for_system_interface(container_name: str) -> None:
    poll = (
        f"for i in $(seq 1 {_SYSTEM_INTERFACE_READY_ATTEMPTS}); do "
        "code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/claude-auth/status); "
        '[ "$code" = "200" ] && exit 0; sleep 5; done; exit 1'
    )
    result = _exec_in_container(container_name, poll, timeout=_SYSTEM_INTERFACE_READY_ATTEMPTS * 5 + 120)
    assert result.returncode == 0, "The workspace's system_interface never answered its claude-auth status endpoint"


def _submit_credentials_via_workspace_endpoint(container_name: str, credential_blob: str) -> dict[str, object]:
    """POST the blob to the workspace's own modal backend (the strict endpoint)."""
    payload = json.dumps({"credentials": credential_blob})
    submit = _exec_in_container(
        container_name,
        "curl -s -X POST http://localhost:8000/api/claude-auth/submit-credentials "
        f"-H 'Content-Type: application/json' -d {json.dumps(payload)}",
        timeout=600,
    )
    assert submit.returncode == 0, f"submit-credentials curl failed: {submit.stderr}"
    body = json.loads(submit.stdout)
    assert isinstance(body, dict), f"submit-credentials returned non-object JSON: {submit.stdout[:500]}"
    return body


def _find_chat_agent_id(container_name: str) -> str:
    listing = _exec_in_container(
        container_name,
        "cd /code && mngr list --format json --on-error continue",
        timeout=_IN_CONTAINER_TIMEOUT_SECONDS,
    )
    assert listing.returncode == 0, f"in-container mngr list failed: {listing.stderr}"
    agents = json.loads(listing.stdout).get("agents", [])
    chat_ids = [str(agent["id"]) for agent in agents if agent.get("type") == "claude"]
    assert chat_ids, f"No claude chat agent among {[a.get('name') for a in agents]!r}"
    return chat_ids[0]


def _chat_and_await_echo(container_name: str, chat_agent_id: str, token: str) -> None:
    messaged = _exec_in_container(
        container_name,
        f'cd /code && mngr message {chat_agent_id} -m "Reply with exactly this token and nothing else: {token}"',
        timeout=300,
    )
    assert messaged.returncode == 0, f"mngr message failed: {messaged.stderr}"
    poll = (
        f"for i in $(seq 1 {_CHAT_REPLY_ATTEMPTS}); do "
        f"cd /code && mngr transcript {chat_agent_id} 2>/dev/null | grep -q {token} && exit 0; "
        "sleep 5; done; exit 1"
    )
    replied = _exec_in_container(container_name, poll, timeout=_CHAT_REPLY_ATTEMPTS * 5 + 120)
    assert replied.returncode == 0, f"The chat agent never echoed the token {token}"


def _await_key_spend(neon_litellm_dsn: str, key_alias: str) -> float:
    """Poll the litellm token table until the minted key shows non-zero spend."""
    for _attempt in range(_SPEND_POLL_ATTEMPTS):
        connection = psycopg2.connect(neon_litellm_dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT COALESCE(spend, 0) FROM "LiteLLM_VerificationToken" WHERE key_alias = %s',
                    (key_alias,),
                )
                rows = cursor.fetchall()
        finally:
            connection.close()
        spends = [float(row[0]) for row in rows]
        if any(spend > 0 for spend in spends):
            return max(spends)
        threading.Event().wait(10)
    raise AssertionError(f"litellm never recorded spend for key alias {key_alias!r}")


@pytest.mark.timeout(2700)
def test_litellm_spend_tracking_via_local_workspace(
    shared_env: Callable[[str], SharedEnvHandle],
    verified_user: VerifiedUserHandle,
    default_workspace_template_ref: DefaultWorkspaceTemplateRef,
) -> None:
    """Drive a real local DEFAULT_WORKSPACE_TEMPLATE workspace + assert spend lands in litellm's ledger.

    Flow (matching the product's post-create sign-in path):

    0. Wait for the env to be reachable (defensive preamble for every test
       in this suite).
    1. Sign in as the pre-verified user and mint a LiteLLM key through the
       connector, with the workspace-keyed alias the desktop app's
       ``/settings/ai-keys`` mint page uses.
    2. Create a real local Docker workspace from the template checkout (the
       same ``mngr create`` invocation the desktop client runs). It boots
       with NO AI credentials.
    3. Submit the env-var credential blob through the workspace's own
       ``/api/claude-auth/submit-credentials`` endpoint (the sign-in
       modal's backend), which writes the shared Claude settings env block
       and restarts the workspace's claude agents.
    4. Send a real chat message via ``mngr message`` and assert the agent
       echoes a unique token -- traffic flows through the minted key + the
       env's litellm proxy.
    5. Poll the env's litellm database for non-zero spend on the minted
       key's alias.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    if shutil.which("docker") is None:
        pytest.skip("Docker is required to create the local workspace")
    if default_workspace_template_ref.worktree_path is None:
        pytest.skip("No local template worktree available (offload sandboxes lack the Docker daemon anyway)")

    # 1. Mint a key for the signed-in user, aliased to the workspace like the
    #    desktop app's mint page does, and render the same paste-ready blob.
    host_name = f"litellm-e2e-{get_short_random_string()}"
    key_alias = f"workspace-{host_name}"
    minted = signin_and_mint_litellm_key(
        connector_url=str(env.urls.connector_url),
        email=str(verified_user.email),
        password=verified_user.password.get_secret_value(),
        key_alias=key_alias,
        max_budget=100.0,
        budget_duration="1d",
    )
    credential_blob = build_credential_blob(api_key=minted.key.get_secret_value(), base_url=str(minted.base_url))

    # 2-5. Create the workspace, sign it in, chat, and assert spend.
    template_path = _prepare_template_clone(default_workspace_template_ref.worktree_path)
    token = get_short_random_string()
    created = False
    try:
        _agent_id, host_id = _create_docker_workspace(template_path, host_name)
        created = True
        container_name = _find_container_name(host_id)
        _wait_for_system_interface(container_name)

        submit_body = _submit_credentials_via_workspace_endpoint(container_name, credential_blob)
        assert submit_body.get("logged_in") is True, f"credential submit did not authenticate: {submit_body!r}"
        assert submit_body.get("auth_mode") == "imbue", f"expected imbue mode after blob submit: {submit_body!r}"

        chat_agent_id = _find_chat_agent_id(container_name)
        _chat_and_await_echo(container_name, chat_agent_id, token)

        spend = _await_key_spend(env.neon_litellm_dsn.get_secret_value(), key_alias)
        logger.info("litellm recorded spend {} for key alias {}", spend, key_alias)
    finally:
        if created:
            destroy = _run(
                ["mngr", "destroy", f"system-services@{host_name}", "--force"],
                cwd=template_path,
                timeout=_CREATE_TIMEOUT_SECONDS,
            )
            if destroy.returncode != 0:
                logger.warning("Workspace teardown failed (leaving for manual cleanup): {}", destroy.stderr[-500:])
        shutil.rmtree(template_path.parent, ignore_errors=True)
