"""End-to-end workspace-sync release tests: real Electron, real connector, real backups.

Each test runs in the minds-snapshot offload sandbox (warm Electron /
Playwright / Xvfb / Docker toolchain) against a REAL deployed connector env
whose coordinates arrive via the ``MINDS_SYNC_E2E_*`` env vars -- forwarded
into the sandbox only on ``run_minds_release_tests`` CI runs (the
``sync_e2e_env`` fixture skips otherwise). Everything after per-test setup is
driven through the real Electron app over Playwright/CDP: sign-in, workspace
association, the master-password settings panel, the landing unlock banner,
and the backups page's snapshot table and download control. The one exception
is backup configuration, which posts the product's own /api/v1
backup-service/configure request from the page because the SPA's Machine
settings Backup group is still a placeholder (see _configure_backups_via_app).
Direct connector reads (via the plugin client) are used only to *wait* for
server-side convergence, never to mutate.

Isolation model: every test gets its own minds root name
(``minds-ci-e2e<rand>``), so the app derives a private data root + mngr host
dir + docker container prefix under the pytest-faked ``$HOME`` -- tests never
touch the snapshot's baked ``minds-ci-snapshot`` workspace and can destroy their
own installs freely. Accounts are per-test (``sync_e2e_account``) under the
env's seeded paid domain, so imbue-cloud backups (R2 provisioning) work.
"""

import json
import os
import shutil
import subprocess
import threading
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Final
from typing import TypeVar

import httpx
import pytest
from argon2 import PasswordHasher
from loguru import logger
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page
from pydantic import AnyUrl
from pydantic import SecretStr
from test_snapshot_resume import _ensure_restic_on_sandbox_host
from test_snapshot_resume import _isolated_host_config_root

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.secret_wrapping import SecretWrappingError
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import mngr_host_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.desktop_client.backup_export import export_zip_path_for_host
from imbue.minds.desktop_client.dek_store import unwrap_bundle_json
from imbue.minds.desktop_client.e2e_workspace_runner import _REPO_ROOT
from imbue.minds.desktop_client.e2e_workspace_runner import _backend_origin_from_page
from imbue.minds.desktop_client.e2e_workspace_runner import _host_id_from_subdomain
from imbue.minds.desktop_client.e2e_workspace_runner import configure_logging
from imbue.minds.desktop_client.e2e_workspace_runner import create_workspace_via_electron
from imbue.minds.desktop_client.e2e_workspace_runner import electron_app_session
from imbue.minds.desktop_client.e2e_workspace_runner import ensure_minds_env_defaults
from imbue.minds.desktop_client.e2e_workspace_runner import find_free_port
from imbue.minds.desktop_client.e2e_workspace_runner import resolve_default_workspace_template_path
from imbue.minds.testing import SyncE2EAccount
from imbue.minds.testing import SyncE2EEnv
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_imbue_cloud.errors import ImbueCloudError
from imbue.mngr_imbue_cloud.wire_types import SyncKeyBundle
from imbue.mngr_imbue_cloud.wire_types import SyncWorkspaceRecord

_SENTINEL_FILENAME: Final[str] = "e2e-backup-sentinel.txt"
# Where the export route writes its zip (backup_export._EXPORT_DIR).
_EXPORT_ZIP_DIR: Final[Path] = Path("/tmp")
_DOCKER_STATE_MARKER: Final[str] = "docker-state"

# Budgets for each UI-observable step, set from what the flows actually take
# (a passing amnesia run is ~5 minutes end to end) plus room for a loaded
# sandbox -- not from fear. The measured cost of each step is noted so a
# future regression shows up as a failure here instead of being absorbed by a
# budget nobody re-derived.
# Headless `auth signin` + the app's account poll listing it on /accounts.
_SIGN_IN_TIMEOUT_SECONDS: Final[int] = 90
# The account reaching the Account group's "Link to <email>" button: measured ~7s.
_ACCOUNT_VISIBLE_TIMEOUT_SECONDS: Final[int] = 90
# A real R2 bucket + scoped key + restic init: measured ~31s.
_BACKUP_CONFIGURE_TIMEOUT_SECONDS: Final[int] = 180
# The first backup reaching the backups page's snapshot table: measured ~10s,
# with two full status-fetch cycles of headroom.
_FIRST_BACKUP_TIMEOUT_SECONDS: Final[int] = 420
# Server-side convergence: the sync scheduler ticks every 60s, so >= 2 ticks.
_SYNC_CONVERGENCE_TIMEOUT_SECONDS: Final[int] = 180
# The unlock banner appearing (needs one pull): measured ~27s.
_UNLOCK_BANNER_TIMEOUT_SECONDS: Final[int] = 180
# The download link un-hiding, gated on one settled status fetch.
_DOWNLOAD_LINK_TIMEOUT_SECONDS: Final[int] = 240
# One backups-page load's history fetch (measured ~10s; see imbue-ai/mngr issue 2470).
_STATUS_FETCH_SETTLE_SECONDS: Final[int] = 120
# The sync scheduler reconciles every 60s; two full ticks with margin is
# enough to observe "the revision did NOT advance". This one is a
# deliberate wait, not a timeout -- it is the cost of the assertion.
_REVISION_QUIET_SECONDS: Final[int] = 150

_T = TypeVar("_T")


class _SyncE2ERuntime(FrozenModel):
    """Per-test app runtime: the private minds root and how to reach everything."""

    root_name: str
    data_root: Path
    mngr_prefix: str
    host_config_root: Path
    template_path: Path
    connector: ImbueCloudConnectorClient


def _prepare_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync_e2e_env: SyncE2EEnv) -> _SyncE2ERuntime:
    """Point the app (and every mngr subprocess it spawns) at a private root + the real env."""
    configure_logging()
    root_name = f"minds-ci-e2e{get_short_random_string()}"
    client_toml = tmp_path / "client.toml"
    client_toml.write_text(
        f'connector_url = "{sync_e2e_env.connector_url}"\nlitellm_proxy_url = "{sync_e2e_env.litellm_proxy_url}"\n'
    )
    monkeypatch.setenv("MINDS_ROOT_NAME", root_name)
    monkeypatch.setenv("MINDS_CLIENT_CONFIG_PATH", str(client_toml))
    # The sandbox has no Modal/AWS creds; silence those providers for every
    # mngr the app spawns. DEFAULT_WORKSPACE_TEMPLATE pins gVisor, absent here.
    monkeypatch.setenv("MNGR__PROVIDERS__MODAL__IS_ENABLED", "false")
    monkeypatch.setenv("MNGR__PROVIDERS__AWS__IS_ENABLED", "false")
    monkeypatch.setenv("MNGR__PROVIDERS__DOCKER__DOCKER_RUNTIME", "runc")
    # The create form POSTs an explicit `runtime`, which wins over the mngr-level
    # env var above; this is the knob that pins the form's own default (the
    # sandbox has no gVisor). Same override test_snapshot_resume.py uses.
    monkeypatch.setenv("MINDS_DOCKER_RUNTIME_DEFAULT", "RUNC")
    monkeypatch.setenv("LATCHKEY_DISABLE_COUNTING", "1")
    ensure_minds_env_defaults(setenv=monkeypatch.setenv)
    return _SyncE2ERuntime(
        root_name=root_name,
        data_root=minds_data_dir_for(root_name),
        mngr_prefix=mngr_prefix_for(root_name),
        host_config_root=_isolated_host_config_root(tmp_path),
        template_path=resolve_default_workspace_template_path(),
        connector=ImbueCloudConnectorClient(base_url=AnyUrl(sync_e2e_env.connector_url)),
    )


def _wait_until(description: str, timeout_seconds: float, probe: Callable[[], _T | None]) -> _T:
    """Poll ``probe`` (None = not yet) until it yields a value, or fail loudly.

    Transient errors from the probe count as "not yet": the Electron content
    view can navigate out from under a Playwright read (the landing page's
    discovering auto-reload, post-auth redirects), and a read-only connector
    poll can hit a transient platform 500 (e.g. Modal's "Server has lost
    track of input"). Deliberate assertion failures raised by probes
    propagate.
    """
    deadline = time.monotonic() + timeout_seconds
    last_transient_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
        except (PlaywrightError, ImbueCloudError, httpx.HTTPError) as e:
            last_transient_error = e
            result = None
        if result is not None:
            return result
        threading.Event().wait(timeout=3.0)
    raise AssertionError(
        f"Timed out after {timeout_seconds}s waiting for {description} (last transient error: {last_transient_error})"
    )


# -- Docker-level setup helpers (pre/post the UI-driven story) ----------------


def _run_docker(args: list[str], *, timeout: int = 60) -> str:
    return subprocess.run(["docker", *args], check=True, capture_output=True, text=True, timeout=timeout).stdout


def _workspace_container_name(runtime: _SyncE2ERuntime) -> str:
    """The test-created workspace's agent container (not the docker-state sidecar)."""
    names = _run_docker(["ps", "--format", "{{.Names}}"]).splitlines()
    matches = [n for n in names if n.startswith(runtime.mngr_prefix) and _DOCKER_STATE_MARKER not in n]
    assert matches, f"No running workspace container with prefix {runtime.mngr_prefix!r}; running: {names!r}"
    return matches[0]


def _write_sentinel_in_container(container_name: str, content: str) -> None:
    """Drop the restore-verification sentinel into the workspace before its first backup.

    ``docker exec`` needs ``-i`` to attach stdin; without it the heredoc lands
    empty and the restore assertion compares empty strings (which is exactly
    how this was caught).
    """
    result = subprocess.run(
        ["docker", "exec", "-i", container_name, "bash", "-lc", f"cat > /home/user/workspace/{_SENTINEL_FILENAME}"],
        input=content,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Could not write the sentinel: {result.stderr}"
    readback = subprocess.run(
        ["docker", "exec", container_name, "bash", "-lc", f"cat /home/user/workspace/{_SENTINEL_FILENAME}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert readback.stdout == content, (
        f"The sentinel did not land in the workspace: {readback.stdout!r} != {content!r}"
    )


def _kill_processes_referencing(unique_marker: str) -> None:
    """Kill host-side processes whose command line names this test's private root.

    A lost machine loses its processes too: stale ``mngr observe``/tmux
    helpers from the pre-wipe app would otherwise survive holding paths under
    the deleted root. The marker is the per-test random root name, so every
    match is unambiguously ours; PIDs are collected first and killed exactly
    (never a broad pkill pattern).
    """
    result = subprocess.run(["pgrep", "-af", unique_marker], capture_output=True, text=True, timeout=30)
    for line in result.stdout.splitlines():
        pid_text, _, command = line.partition(" ")
        if not pid_text.isdigit() or unique_marker not in command:
            continue
        logger.info("Killing stale process {} from the wiped install: {}", pid_text, command[:160])
        subprocess.run(["kill", pid_text], capture_output=True, text=True, timeout=10)


def _wipe_local_install(runtime: _SyncE2ERuntime) -> None:
    """Simulate total machine loss: no minds data, no mngr host dir, no processes, no containers."""
    logger.info("Wiping local install: {} and containers with prefix {}", runtime.data_root, runtime.mngr_prefix)
    _kill_processes_referencing(runtime.root_name)
    shutil.rmtree(runtime.data_root, ignore_errors=True)
    container_ids = _run_docker(["ps", "-aq", "--filter", f"name={runtime.mngr_prefix}"]).split()
    if container_ids:
        _run_docker(["rm", "-f", *container_ids], timeout=120)


def _destroy_test_containers_best_effort(runtime: _SyncE2ERuntime) -> None:
    """Teardown: never leak this test's containers into the shared sandbox docker."""
    try:
        container_ids = _run_docker(["ps", "-aq", "--filter", f"name={runtime.mngr_prefix}"]).split()
        if container_ids:
            _run_docker(["rm", "-f", *container_ids], timeout=120)
    except (subprocess.SubprocessError, OSError) as e:
        logger.warning("Container cleanup for prefix {} failed: {}", runtime.mngr_prefix, e)


def _destroy_account_buckets_best_effort(runtime: _SyncE2ERuntime, account: SyncE2EAccount) -> None:
    """Teardown: try to remove the R2 buckets imbue-cloud backups provisioned.

    Cloudflare refuses to delete a non-empty bucket, and a bucket that
    received a real restic backup is non-empty -- so this logs (rather than
    fails on) buckets it cannot remove. The per-test account owns only this
    test's buckets, so the log line identifies exactly what leaked.
    """
    try:
        buckets = runtime.connector.list_buckets(account.access_token)
    except (ImbueCloudError, httpx.HTTPError, OSError) as e:
        # Teardown must never mask the test result; any listing failure is logged.
        logger.warning("Could not list buckets for cleanup: {}", e)
        return
    for bucket in buckets:
        try:
            runtime.connector.destroy_bucket(account.access_token, bucket.bucket_name)
            logger.info("Deleted test bucket {}", bucket.bucket_name)
        except (ImbueCloudError, httpx.HTTPError, OSError) as e:
            # Cloudflare refuses non-empty deletes; log the leak with its name.
            logger.warning("Could not delete test bucket {} (likely non-empty): {}", bucket.bucket_name, e)


# -- Connector convergence probes (read-only waits, never mutations) -----------


def _record_for_agent(runtime: _SyncE2ERuntime, account: SyncE2EAccount, agent_id: str) -> SyncWorkspaceRecord | None:
    for record in runtime.connector.list_sync_records(account.access_token):
        if record.agent_id == agent_id:
            return record
    return None


def _wait_for_synced_secrets(
    runtime: _SyncE2ERuntime, account: SyncE2EAccount, agent_id: str, timeout_seconds: float
) -> SyncWorkspaceRecord:
    def probe() -> SyncWorkspaceRecord | None:
        record = _record_for_agent(runtime, account, agent_id)
        if record is not None and record.encrypted_secrets is not None:
            return record
        return None

    return _wait_until(f"synced record with secrets for {agent_id}", timeout_seconds, probe)


def _wait_for_bundle(runtime: _SyncE2ERuntime, account: SyncE2EAccount, timeout_seconds: float) -> SyncKeyBundle:
    return _wait_until(
        "the account key bundle on the connector",
        timeout_seconds,
        lambda: runtime.connector.get_key_bundle(account.access_token),
    )


def _wait_for_rewrapped_bundle(
    runtime: _SyncE2ERuntime, account: SyncE2EAccount, previous_wrapped_dek: str, timeout_seconds: float
) -> SyncKeyBundle:
    """Wait for the connector bundle's wrapped key to differ from the previous one."""

    def probe() -> SyncKeyBundle | None:
        bundle = runtime.connector.get_key_bundle(account.access_token)
        if bundle is not None and bundle.wrapped_dek != previous_wrapped_dek:
            return bundle
        return None

    return _wait_until("the rewrapped bundle to land on the connector", timeout_seconds, probe)


def _unwrapped_dek(bundle: SyncKeyBundle, password: str) -> bytes:
    """Unwrap the bundle with ``password`` (raises SecretWrappingError when wrong)."""
    return unwrap_bundle_json(bundle.model_dump(), SecretStr(password))


# -- Electron UI flows ---------------------------------------------------------


def _create_unassociated_workspace(runtime: _SyncE2ERuntime) -> str:
    """Drive the real create form (signed out, local preset) and return the agent id."""
    workspace_name = f"synce2e-{get_short_random_string()}"
    created_host_ids: list[str] = []
    create_workspace_via_electron(
        runtime.template_path,
        workspace_name,
        find_free_port(),
        host_config_dir=runtime.host_config_root,
        on_workspace_ready=lambda page: created_host_ids.append(_host_id_from_subdomain(page.url)),
    )
    assert created_host_ids, "The create flow finished without a workspace URL"
    agent_id = _agent_id_for_host_id(runtime, created_host_ids[0])
    logger.info("Created workspace {} -> {} (host {})", workspace_name, agent_id, created_host_ids[0])
    return agent_id


def _agent_id_for_host_id(runtime: _SyncE2ERuntime, host_id: str) -> str:
    """Map the workspace host coordinate (from content URLs) to its agent id via ``mngr list``.

    Content URLs are host-keyed while the app's records, settings routes, and
    sync records stay agent-keyed, so the flow needs this translation once.
    """
    # ``--on-error continue`` with the provider-inaccessible exit code (6)
    # tolerated: the offload sandbox runs as root, where limactl refuses to
    # start, so the configured lima provider is always inaccessible there; the
    # default abort mode would fail the whole list on it, while continue mode
    # still emits every reachable provider's agents and signals the partial
    # failure through the exit code.
    # Same invocation shape as _sign_in_headless's mngr subprocess: the APP's
    # own MNGR_HOST_DIR (where its agents' host records live -- pointing at the
    # isolated config root instead returns an empty listing), with the config
    # root as cwd so the project-config walk finds the pytest-opted-in
    # settings.toml.
    result = subprocess.run(
        ["uv", "run", "--project", str(_REPO_ROOT), "mngr", "list", "--format", "json", "--on-error", "continue"],
        env={
            **os.environ,
            "MNGR_HOST_DIR": str(mngr_host_dir_for(runtime.root_name)),
            "MNGR_PREFIX": runtime.mngr_prefix,
        },
        cwd=runtime.host_config_root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode in (0, 6), f"`mngr list` failed (exit {result.returncode}):\n{result.stderr[-4000:]}"
    data = json.loads(result.stdout)
    for raw in data.get("agents", []) if isinstance(data, dict) else []:
        host = raw.get("host") if isinstance(raw.get("host"), dict) else {}
        if host.get("id") == host_id and raw.get("id"):
            return str(raw["id"])
    raise AssertionError(f"No agent on host {host_id!r} in `mngr list` output:\n{result.stdout[:4000]}")


def _sign_in_headless(runtime: _SyncE2ERuntime, page: Page, email: str, password: str) -> str:
    """Sign in via the headless ``auth signin`` CLI; returns the backend origin.

    The in-app email/password form was replaced by the hosted browser flow
    (``mngr imbue_cloud auth login``), which opens a real system browser this
    sandbox cannot drive; the hosted pages themselves are covered by the
    connector deployment tests (apps/minds/deployment_tests/test_accounts_web.py).
    The headless signin writes the same on-disk session store the app's own
    ``mngr imbue_cloud`` subprocesses read, so the /accounts gate below still
    proves the app discovered the session.

    The subprocess runs from ``runtime.host_config_root`` (whose settings.toml
    carries the pytest config-guard opt-in; the repo root's does not) with the
    app's own MNGR_HOST_DIR/MNGR_PREFIX, after waiting for the app to have
    initialized that mngr profile (the plugin's session store needs its
    config.toml).
    """
    origin = _backend_origin_from_page(page)
    profile_config = mngr_host_dir_for(runtime.root_name) / "config.toml"
    _wait_until(
        f"the app's mngr profile config at {profile_config}",
        _SIGN_IN_TIMEOUT_SECONDS,
        lambda: True if profile_config.is_file() else None,
    )
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(_REPO_ROOT),
            "mngr",
            "imbue_cloud",
            "auth",
            "signin",
            "--account",
            email,
            "--password",
            password,
            "--connector-url",
            str(runtime.connector.base_url).rstrip("/"),
        ],
        env={
            **os.environ,
            "MNGR_HOST_DIR": str(mngr_host_dir_for(runtime.root_name)),
            "MNGR_PREFIX": runtime.mngr_prefix,
        },
        cwd=runtime.host_config_root,
        capture_output=True,
        text=True,
        timeout=_SIGN_IN_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, (
        f"auth signin for {email} failed (exit {result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
    )

    def account_listed() -> bool | None:
        page.goto(f"{origin}/accounts", wait_until="domcontentloaded")
        return True if email in page.inner_text("body") else None

    _wait_until(f"the signed-in account {email} to appear on /accounts", _SIGN_IN_TIMEOUT_SECONDS, account_listed)
    logger.info("Signed in as {}", email)
    return origin


def _account_group_url(origin: str, agent_id: str) -> str:
    """The options overlay's Machine settings Account group (SettingsGroups.ts)."""
    return f"{origin}/workspace/{agent_id}/options?tab=settings&group=account"


def _account_section_text(page: Page) -> str:
    """The Account group's section text once the options data has loaded.

    The SPA renders `#account-section` only after its /ui/api options fetch
    resolves; the short in-page wait covers that load, and its timeout raises
    a PlaywrightError the surrounding ``_wait_until`` probes treat as "not
    yet" (they re-navigate, restarting a hung fetch).
    """
    page.wait_for_selector("#account-section", timeout=10_000)
    return page.inner_text("#account-section")


def _associate_workspace_via_ui(page: Page, origin: str, agent_id: str, email: str) -> None:
    """Associate the workspace with the signed-in account from its Machine settings.

    The Account group offers one "Link to <email>" button per signed-in
    account (SettingsGroups.renderAssociatePrompt); linking swaps the prompt
    for a "Linked to <email>." line with an Unlink control.
    """
    account_url = _account_group_url(origin, agent_id)
    link_button_selector = f'#account-section button:has-text("Link to {email}")'

    def account_link_ready() -> bool | None:
        page.goto(account_url, wait_until="domcontentloaded")
        _account_section_text(page)
        return True if page.query_selector(link_button_selector) is not None else None

    _wait_until(f"the Account group to offer linking to {email}", _ACCOUNT_VISIBLE_TIMEOUT_SECONDS, account_link_ready)
    page.click(link_button_selector)
    # The link click PATCHes the association from the page itself; navigating
    # away immediately would abort that in-flight request, so wait for the
    # same page to flip to the linked state first.
    page.wait_for_selector('#account-section:has-text("Linked to")', timeout=60_000)

    def associated() -> bool | None:
        page.goto(account_url, wait_until="domcontentloaded")
        section_text = _account_section_text(page)
        return True if ("Linked to" in section_text and email in section_text) else None

    _wait_until(f"the Account group to show {email} as the linked account", 60, associated)
    logger.info("Associated {} with {}", agent_id, email)


def _configure_backups_via_app(
    page: Page, origin: str, agent_id: str, provider: str, api_key_env: str | None = None
) -> None:
    """Configure backups through the app's backup-service API and wait for provisioning.

    The SPA's Machine settings Backup group is still the placeholder from the
    frontend port (BackupGroupSlot.ts: "The backups section is being rebuilt"),
    so there is no in-app configure form to drive yet. Until that tranche
    lands, this posts the same ``/api/v1/.../backup-service/configure`` request
    the form will send -- from the app's own page, on its real session cookies
    -- and then polls the dispatched ``backup_configure`` operation the same
    way the backups page's operation strip does.
    """
    page.goto(f"{origin}/", wait_until="domcontentloaded")
    body: dict[str, str] = {"backup_provider": provider}
    if api_key_env is not None:
        body["api_key_env"] = api_key_env
    dispatch = page.evaluate(
        """(args) => fetch(`/api/v1/workspaces/${encodeURIComponent(args.agentId)}/backup-service/configure`, {
               method: 'POST',
               credentials: 'same-origin',
               headers: {'Content-Type': 'application/json'},
               body: JSON.stringify(args.body),
           }).then((resp) => resp.text().then((text) => ({status: resp.status, body: text.slice(0, 1000)})))""",
        {"agentId": agent_id, "body": body},
    )
    assert dispatch["status"] == 202, f"Backup configure dispatch for {agent_id} failed: {dispatch}"

    def provisioned() -> bool | None:
        payload = page.evaluate(
            """(aid) => fetch(`/api/v1/workspaces/operations/backup/${encodeURIComponent(aid)}`, {
                   credentials: 'same-origin',
               }).then((resp) => resp.json().then((body) => ({status: resp.status, body})))""",
            agent_id,
        )
        if payload["status"] != 200:
            return None
        operation = payload["body"]
        if operation.get("status") == "RUNNING":
            return None
        if operation.get("is_done") is not True:
            raise AssertionError(f"Backup configuration failed for {agent_id}: {operation}")
        return True

    _wait_until(
        f"backup provisioning ({provider}) to finish for {agent_id}",
        _BACKUP_CONFIGURE_TIMEOUT_SECONDS,
        provisioned,
    )
    logger.info("Backups configured ({}) for {}", provider, agent_id)


def _set_master_password_via_ui(page: Page, origin: str, new_password: str) -> None:
    """Change (or clear, with an empty string) the master password on /settings.

    ``?section=backups`` opens the Master password panel directly (the same
    deep link the menu bar uses; SettingsPage.selectRequestedSection). The
    panel has no result-element ids, so the probe reads the panel's error
    alert and per-account results list (SettingsSections.masterPasswordPanel).
    """
    panel_scope = "section:has(#backup-new-password)"
    page.goto(f"{origin}/settings?section=backups", wait_until="domcontentloaded")
    page.wait_for_selector("#backup-new-password", state="visible", timeout=15_000)
    page.fill("#backup-new-password", new_password)
    page.fill("#backup-new-password-confirm", new_password)
    page.click("#backup-change-password-btn")

    def change_reported() -> bool | None:
        error_element = page.query_selector(f'{panel_scope} p[role="alert"]')
        error_text = error_element.inner_text() if error_element is not None else ""
        if error_text.strip():
            raise AssertionError(f"Master password change surfaced an error: {error_text.strip()}")
        results = page.query_selector(f'{panel_scope} ul[aria-live="polite"]')
        if results is None:
            return None
        results_text = results.inner_text()
        # Failure shapes: "<account>: FAILED - ...", "The master password
        # change failed.", "Re-run the change to retry the failed accounts."
        assert "failed" not in results_text.lower(), f"Master password change reported a failure: {results_text}"
        return True

    _wait_until("the master password change to report success", 120, change_reported)
    logger.info("Master password {} via settings", "cleared" if new_password == "" else "updated")


def _goto_landing(page: Page, origin: str) -> None:
    """Open the landing page.

    No consent detour: the SPA shows the first-run error-reporting notice only
    on its own /consent route, which nothing but the Electron shell's
    cold-start first-window routing opens (the legacy frontend's server-side
    gate on ``/`` is gone), so an explicit load of ``/`` always renders the
    landing -- and nothing these tests drive is gated on acknowledging it.
    """
    page.goto(f"{origin}/", wait_until="domcontentloaded")


# The snapshot table's per-row Download control (SnapshotTable.ts; the rows
# are newest-first, so the first match is the latest snapshot).
_DOWNLOAD_BUTTON_SELECTOR: Final[str] = 'button:text-is("Download")'


def _read_settled_backups_listing(page: Page, origin: str, agent_id: str) -> str:
    """One backups-page load, waited on until its history fetch settles; returns the body text.

    The page loads its snapshot listing once on mount, and that fetch runs
    restic against the (possibly remote) repository before the route responds
    (imbue-ai/mngr issue 2470). Reloading before it resolves aborts and
    restarts it, so this reads WITHOUT navigating until the "Loading backup
    history..." status leaves. The window is generous against the measured
    ~10s fetch but deliberately well below that route's worst case: if that
    latency ever regresses this fails in minutes rather than hanging.
    """
    page.goto(f"{origin}/workspace/{agent_id}/backups", wait_until="domcontentloaded")
    deadline = time.monotonic() + _STATUS_FETCH_SETTLE_SECONDS
    body_text = page.inner_text("body")
    while time.monotonic() < deadline:
        if "Loading backup history" not in body_text and "Backups" in body_text:
            return body_text
        page.wait_for_timeout(3_000)
        body_text = page.inner_text("body")
    return body_text


def _timed_status_fetch(page: Page, agent_id: str) -> str:
    """Diagnostic: how long one full backups-status fetch takes and what it returns."""
    started = time.monotonic()
    try:
        result = page.evaluate(
            """(aid) => fetch('/api/v1/workspaces/' + aid + '/backups')
                .then((resp) => resp.text().then((body) => ({status: resp.status, body: body.slice(0, 600)})))""",
            agent_id,
        )
    except PlaywrightError as e:
        return f"status fetch failed after {time.monotonic() - started:.0f}s: {e}"
    return f"status fetch took {time.monotonic() - started:.0f}s -> {result}"


def _container_backup_diagnostics(container_name: str) -> str:
    """Tail of the workspace's backup service state, for badge-timeout failures."""
    parts: list[str] = []
    for label, command in (
        ("supervisor", "supervisorctl status host-backup"),
        (
            "events",
            "tail -c 3000 /home/user/.mngr/agents/*/events/backup/events.jsonl 2>/dev/null || echo no-events-file",
        ),
        ("env", "test -f /home/user/workspace/data/.secrets/restic.env && echo env-present || echo env-missing"),
    ):
        result = subprocess.run(
            ["docker", "exec", container_name, "bash", "-lc", command],
            capture_output=True,
            text=True,
            timeout=60,
        )
        parts.append(f"[{label}] {(result.stdout or result.stderr).strip()[:1200]}")
    return " | ".join(parts)


def _wait_for_first_backup_listed(page: Page, origin: str, agent_id: str, container_name: str) -> None:
    """Reload the workspace backups page until its snapshot table lists a completed backup.

    The SPA landing rows carry only an unwired badge slot for backup status
    (LandingPage.ts: "Slot for the backup badge"), so the observable "first
    backup finished" product surface is the /workspace/<id>/backups snapshot
    table gaining its first row.
    """

    def backed_up() -> bool | None:
        body_text = _read_settled_backups_listing(page, origin, agent_id)
        if page.query_selector(_DOWNLOAD_BUTTON_SELECTOR) is not None:
            return True
        logger.info("Backups page for {} has no snapshot rows yet: {!r}", agent_id, " ".join(body_text.split())[:200])
        return None

    try:
        _wait_until(
            f"the backups page to list a completed backup for {agent_id}",
            _FIRST_BACKUP_TIMEOUT_SECONDS,
            backed_up,
        )
    except AssertionError as e:
        try:
            container_state = _container_backup_diagnostics(container_name)
        except (subprocess.SubprocessError, OSError) as diag_error:
            container_state = f"(container diagnostics unavailable: {diag_error})"
        fetch_timing = _timed_status_fetch(page, agent_id)
        raise AssertionError(f"{e}; {container_state}; {fetch_timing}") from None
    logger.info("The backups page lists a completed backup for {}", agent_id)


def _landing_state_snapshot(page: Page) -> str:
    """Best-effort description of what the landing page currently shows (for failures)."""
    try:
        body_text = " ".join(page.inner_text("body").split())[:500]
        agent_ids = page.eval_on_selector_all(
            "[data-agent-id]", "els => els.map(e => e.getAttribute('data-agent-id'))"
        )
        return f"landing cards={agent_ids} body={body_text!r}"
    except PlaywrightError as e:
        return f"(snapshot unavailable: {e})"


def _wait_for_unlock_banner(page: Page, origin: str) -> None:
    def banner_present() -> bool | None:
        _goto_landing(page, origin)
        return True if page.query_selector("#sync-unlock-banner") is not None else None

    try:
        _wait_until(
            "the sync unlock banner to appear on the landing page", _UNLOCK_BANNER_TIMEOUT_SECONDS, banner_present
        )
    except AssertionError as e:
        raise AssertionError(f"{e}; {_landing_state_snapshot(page)}") from None


def _unlock_via_banner(page: Page, origin: str, password: str, expect_success: bool = True) -> None:
    """Drive the landing unlock banner; asserts the expected outcome.

    The unlock POST runs from the page itself, so both outcomes are awaited
    in place first (navigating away would abort the in-flight request): on
    success the banner unmounts once the reloaded extras drop the locked
    account, on failure its inline alert appears (LandingPage.ts).
    """
    _wait_for_unlock_banner(page, origin)
    page.fill("#sync-unlock-password", password)
    page.click("#sync-unlock-btn")
    if expect_success:
        page.wait_for_selector("#sync-unlock-banner", state="detached", timeout=60_000)

        def banner_gone() -> bool | None:
            _goto_landing(page, origin)
            return True if page.query_selector("#sync-unlock-banner") is None else None

        _wait_until("the unlock banner to stay cleared after unlocking", 60, banner_gone)
        logger.info("Unlocked synced workspaces via the banner")
    else:
        page.wait_for_selector('#sync-unlock-banner p[role="alert"]', state="visible", timeout=30_000)
        logger.info("Wrong password was refused by the unlock banner, as expected")


def _assert_remote_row_visible(page: Page, origin: str, agent_id: str) -> None:
    """The workspace renders as a greyed other-device row with a remove control."""

    def remote_row() -> bool | None:
        _goto_landing(page, origin)
        card = page.query_selector(f'[data-agent-id="{agent_id}"]')
        if card is None:
            return None
        remove_button = card.query_selector('[aria-label="Remove from this list"]')
        return True if remove_button is not None else None

    try:
        _wait_until(f"a remote-device landing row for {agent_id}", 120, remote_row)
    except AssertionError as e:
        raise AssertionError(f"{e}; {_landing_state_snapshot(page)}") from None


def _download_backup_zip(page: Page, origin: str, agent_id: str, dest_dir: Path) -> Path:
    """Click Download on the workspace backups page's snapshot table and return the zip path.

    Electron's content view does not surface Playwright download events over
    CDP (the click lands in the renderer's own blob-save handling), and the
    export is ~100 MB -- far too large to verify through the renderer. So the
    click is the real product action, and the artifact we verify is the file
    the export route itself produced for that click: ``export_zip_path_for_host``
    names it deterministically, and the route streams exactly those bytes to
    the browser. Waiting for it to appear (fresh mtime) proves the click ran
    the whole restore-and-zip path.
    """

    def download_visible() -> bool | None:
        # One settled listing per attempt (the listing only loads on mount,
        # so a fresh navigation is what re-checks for rows). The first
        # Download is the newest snapshot.
        _read_settled_backups_listing(page, origin, agent_id)
        return True if page.query_selector(_DOWNLOAD_BUTTON_SELECTOR) is not None else None

    _wait_until(
        f"the backup Download control for {agent_id} on the backups page",
        _DOWNLOAD_LINK_TIMEOUT_SECONDS,
        download_visible,
    )

    # The route keys the zip by the workspace's host id, falling back to the
    # agent id when local discovery does not know the workspace -- which is
    # exactly this (post-wipe, remote-record) case. Accept either name.
    candidate_paths = (export_zip_path_for_host(agent_id), *sorted(_EXPORT_ZIP_DIR.glob("minds-backup-export-*.zip")))
    stale_mtimes = {path: path.stat().st_mtime for path in candidate_paths if path.exists()}
    clicked_at = time.time()
    page.click(_DOWNLOAD_BUTTON_SELECTOR)
    logger.info("Clicked the backup Download control for {} on the backups page", agent_id)

    def exported() -> Path | None:
        for path in (export_zip_path_for_host(agent_id), *_EXPORT_ZIP_DIR.glob("minds-backup-export-*.zip")):
            if not path.exists():
                continue
            stats = path.stat()
            if stats.st_mtime <= stale_mtimes.get(path, 0.0) or stats.st_mtime < clicked_at - 5:
                continue
            # The route builds the zip before streaming it, but guard against
            # reading one still being written: require a stable, non-zero size.
            first_size = stats.st_size
            page.wait_for_timeout(2_000)
            if first_size > 0 and path.stat().st_size == first_size:
                return path
        return None

    zip_path = _wait_until(f"the export route to produce a backup zip for {agent_id}", 300, exported)
    saved_path = dest_dir / f"{agent_id}-backup.zip"
    shutil.copyfile(zip_path, saved_path)
    logger.info("Backup export produced {} ({} bytes)", zip_path, saved_path.stat().st_size)
    return saved_path


def _assert_zip_contains_sentinel(zip_path: Path, sentinel_content: str) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        matches = [name for name in archive.namelist() if name.endswith(_SENTINEL_FILENAME)]
        assert matches, (
            f"The restored backup zip has no {_SENTINEL_FILENAME}; first entries: {archive.namelist()[:40]}"
        )
        restored = archive.read(matches[0]).decode("utf-8")
        assert restored == sentinel_content, (
            f"The restored sentinel does not match: {restored!r} != {sentinel_content!r}"
        )


# -- The tests -----------------------------------------------------------------


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.rsync
# ~3x the measured 5-minute runtime, and strictly below offload's
# test_timeout_secs so an overrun fails INSIDE pytest (junit + failure
# diagnostics survive) instead of being killed by the sandbox.
# func_only=False covers fixture time too (the config default exempts it).
@pytest.mark.timeout(900, func_only=False)
def test_amnesia_and_recover_full_lifecycle_via_electron(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_e2e_env: SyncE2EEnv,
    sync_e2e_account: SyncE2EAccount,
    snapshot_sandbox_dockerd: None,
    xvfb_display: str,
) -> None:
    """Total machine loss and recovery, end to end through the product.

    Create a local docker workspace, sign in, configure imbue-cloud backups
    (real R2 bucket + restic repo), set a master password, and let backups +
    sync converge. Then simulate losing the machine (quit the app, delete the
    entire local data root and mngr host dir, remove the docker containers),
    reinstall (fresh app), sign back in, unlock with the master password via
    the landing banner, and download the old workspace's backup from its
    Backups page (per-snapshot Download) -- verifying a sentinel file
    round-tripped byte-for-byte through R2.
    """
    runtime = _prepare_runtime(tmp_path, monkeypatch, sync_e2e_env)
    # The landing badge's status listing and the backup export both run restic
    # from the sandbox host (not the workspace container), and the snapshot
    # image carries no restic binary.
    _ensure_restic_on_sandbox_host(tmp_path, monkeypatch)
    master_password = f"master-{get_short_random_string()}"
    sentinel_content = f"sync-e2e sentinel {get_short_random_string()}\n"

    try:
        agent_id = _create_unassociated_workspace(runtime)
        container_name = _workspace_container_name(runtime)
        _write_sentinel_in_container(container_name, sentinel_content)

        with electron_app_session(runtime.template_path, find_free_port(), runtime.host_config_root) as (
            _browser,
            page,
        ):
            origin = _sign_in_headless(
                runtime, page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value()
            )
            _associate_workspace_via_ui(page, origin, agent_id, sync_e2e_account.email)
            _configure_backups_via_app(page, origin, agent_id, "IMBUE_CLOUD")
            _set_master_password_via_ui(page, origin, master_password)
            _wait_for_first_backup_listed(page, origin, agent_id, container_name)

        # Convergence gates before pulling the plug: the record's secrets and
        # the wrapped key are on the server (read-only connector waits).
        record = _wait_for_synced_secrets(runtime, sync_e2e_account, agent_id, _SYNC_CONVERGENCE_TIMEOUT_SECONDS)
        bundle = _wait_for_bundle(runtime, sync_e2e_account, _SYNC_CONVERGENCE_TIMEOUT_SECONDS)
        _unwrapped_dek(bundle, master_password)
        logger.info("Converged: record revision {} with secrets, bundle present; wiping the install", record.revision)

        _wipe_local_install(runtime)

        with electron_app_session(runtime.template_path, find_free_port(), runtime.host_config_root) as (
            _browser,
            page,
        ):
            origin = _sign_in_headless(
                runtime, page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value()
            )
            _unlock_via_banner(page, origin, f"wrong-{master_password}", expect_success=False)
            _unlock_via_banner(page, origin, master_password)
            _assert_remote_row_visible(page, origin, agent_id)
            zip_path = _download_backup_zip(page, origin, agent_id, tmp_path)

        _assert_zip_contains_sentinel(zip_path, sentinel_content)
    finally:
        _destroy_test_containers_best_effort(runtime)
        _destroy_account_buckets_best_effort(runtime, sync_e2e_account)


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(900, func_only=False)
def test_legacy_association_files_migrate_into_synced_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_e2e_env: SyncE2EEnv,
    sync_e2e_account: SyncE2EAccount,
    snapshot_sandbox_dockerd: None,
    xvfb_display: str,
) -> None:
    """A pre-sync install's local files convert into server records on first sign-in.

    Fabricates the legacy layout (``workspace_associations.json`` naming a
    real local workspace, ``backup_password`` + ``backup_password_hash``, and
    a canonical restic env) before the app starts; then signs in through the
    real UI and asserts the one-time migration pushed a record with encrypted
    secrets, pushed a bundle that unwraps with the legacy password, retired
    every legacy file with the ``.pre-sync`` suffix, and settled (no revision
    churn). Finally proves the legacy password IS the master password by
    unlocking a fresh install with it.
    """
    runtime = _prepare_runtime(tmp_path, monkeypatch, sync_e2e_env)
    legacy_password = f"legacy-{get_short_random_string()}"

    try:
        agent_id = _create_unassociated_workspace(runtime)

        # Fabricate the pre-sync generation's on-disk state (setup, pre-start).
        runtime.data_root.mkdir(parents=True, exist_ok=True)
        (runtime.data_root / "workspace_associations.json").write_text(
            json.dumps({sync_e2e_account.user_id: [agent_id]})
        )
        (runtime.data_root / "backup_password").write_text(legacy_password + "\n")
        (runtime.data_root / "backup_password_hash").write_text(PasswordHasher().hash(legacy_password))
        backup_envs_dir = runtime.data_root / "backup_envs"
        backup_envs_dir.mkdir(parents=True, exist_ok=True)
        (backup_envs_dir / f"{agent_id}.env").write_text(
            f"RESTIC_REPOSITORY={tmp_path / 'legacy-repo'}\nRESTIC_PASSWORD=ws-{get_short_random_string()}\n"
        )

        with electron_app_session(runtime.template_path, find_free_port(), runtime.host_config_root) as (
            _browser,
            page,
        ):
            origin = _sign_in_headless(
                runtime, page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value()
            )

            record = _wait_for_synced_secrets(runtime, sync_e2e_account, agent_id, _SYNC_CONVERGENCE_TIMEOUT_SECONDS)
            bundle = _wait_for_bundle(runtime, sync_e2e_account, _SYNC_CONVERGENCE_TIMEOUT_SECONDS)
            _unwrapped_dek(bundle, legacy_password)
            with pytest.raises(SecretWrappingError):
                _unwrapped_dek(bundle, "not-the-legacy-password")

            # The legacy files were retired, not deleted.
            assert not (runtime.data_root / "workspace_associations.json").exists()
            assert (runtime.data_root / "workspace_associations.json.pre-sync").exists()
            assert not (runtime.data_root / "backup_password").exists()
            assert (runtime.data_root / "backup_password.pre-sync").exists()
            assert not (runtime.data_root / "backup_password_hash").exists()
            assert (runtime.data_root / "backup_password_hash.pre-sync").exists()

            # The workspace shows as associated in the real settings UI: the
            # Account group renders the linked state (never the associate
            # prompt) once the migration landed. Polled, not asserted on the
            # first paint: the SPA fills `#account-section` only after its
            # asynchronous options fetch resolves.
            def migration_shown_as_linked() -> bool | None:
                page.goto(_account_group_url(origin, agent_id), wait_until="domcontentloaded")
                section_text = _account_section_text(page)
                if "Linked to" in section_text and sync_e2e_account.email in section_text:
                    return True
                return None

            _wait_until(
                f"the Account group to show {sync_e2e_account.email} as linked post-migration",
                60,
                migration_shown_as_linked,
            )
            assert "Link to" not in page.inner_text("#account-section"), (
                "The associate prompt should be gone post-migration"
            )

            # Reconcile settles: the revision must not creep while we watch.
            settled_revision = record.revision
            threading.Event().wait(timeout=_REVISION_QUIET_SECONDS)
            record_after = _record_for_agent(runtime, sync_e2e_account, agent_id)
            assert record_after is not None
            assert record_after.revision == settled_revision, (
                f"Revision churn after migration: {settled_revision} -> {record_after.revision}"
            )

        # The legacy password is now the master password: a fresh install unlocks with it.
        _wipe_local_install(runtime)
        with electron_app_session(runtime.template_path, find_free_port(), runtime.host_config_root) as (
            _browser,
            page,
        ):
            origin = _sign_in_headless(
                runtime, page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value()
            )
            _unlock_via_banner(page, origin, legacy_password)
            _assert_remote_row_visible(page, origin, agent_id)
    finally:
        _destroy_test_containers_best_effort(runtime)


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(900, func_only=False)
def test_master_password_lifecycle_rewraps_scrubs_and_restores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sync_e2e_env: SyncE2EEnv,
    sync_e2e_account: SyncE2EAccount,
    snapshot_sandbox_dockerd: None,
    xvfb_display: str,
) -> None:
    """The master password's whole lifecycle against the real connector.

    With a workspace synced under password P1: changing to P2 is rewrap-only
    (the server's secrets blob is byte-identical, its revision unchanged, P1
    stops unwrapping and P2 unwraps the SAME key); clearing the password
    deletes the server bundle and scrubs every record's secrets while this
    (hosting, unlocked) install keeps working; setting P3 pushes a fresh
    bundle and re-pushes the pending secrets. A fresh install then unlocks
    with P3 via the landing banner.

    Backups use the API_KEY provider against a local restic repository --
    password mechanics are independent of the storage backend, and this keeps
    the test off the R2 budget.
    """
    runtime = _prepare_runtime(tmp_path, monkeypatch, sync_e2e_env)
    _ensure_restic_on_sandbox_host(tmp_path, monkeypatch)
    password_one = f"first-{get_short_random_string()}"
    password_two = f"second-{get_short_random_string()}"
    password_three = f"third-{get_short_random_string()}"

    try:
        agent_id = _create_unassociated_workspace(runtime)

        with electron_app_session(runtime.template_path, find_free_port(), runtime.host_config_root) as (
            _browser,
            page,
        ):
            origin = _sign_in_headless(
                runtime, page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value()
            )
            _associate_workspace_via_ui(page, origin, agent_id, sync_e2e_account.email)
            _configure_backups_via_app(
                page, origin, agent_id, "API_KEY", api_key_env=f"RESTIC_REPOSITORY={tmp_path / 'pw-repo'}"
            )
            _set_master_password_via_ui(page, origin, password_one)

            record_one = _wait_for_synced_secrets(
                runtime, sync_e2e_account, agent_id, _SYNC_CONVERGENCE_TIMEOUT_SECONDS
            )
            bundle_one = _wait_for_bundle(runtime, sync_e2e_account, _SYNC_CONVERGENCE_TIMEOUT_SECONDS)
            dek = _unwrapped_dek(bundle_one, password_one)

            # P1 -> P2 is a rewrap: same key, same secrets blob, same revision.
            _set_master_password_via_ui(page, origin, password_two)
            bundle_two = _wait_for_rewrapped_bundle(
                runtime, sync_e2e_account, bundle_one.wrapped_dek, _SYNC_CONVERGENCE_TIMEOUT_SECONDS
            )
            assert _unwrapped_dek(bundle_two, password_two) == dek
            with pytest.raises(SecretWrappingError):
                _unwrapped_dek(bundle_two, password_one)
            record_two = _record_for_agent(runtime, sync_e2e_account, agent_id)
            assert record_two is not None
            assert record_two.encrypted_secrets == record_one.encrypted_secrets, (
                "A password change must not rewrite the synced secrets blob"
            )
            assert record_two.revision == record_one.revision, (
                f"A password change must not advance the record revision "
                f"({record_one.revision} -> {record_two.revision})"
            )

            # Clearing the password deletes the bundle and scrubs the secrets.
            _set_master_password_via_ui(page, origin, "")

            def scrubbed() -> bool | None:
                if runtime.connector.get_key_bundle(sync_e2e_account.access_token) is not None:
                    return None
                record = _record_for_agent(runtime, sync_e2e_account, agent_id)
                if record is None or record.encrypted_secrets is not None:
                    return None
                return True

            _wait_until(
                "the bundle to disappear and the secrets to scrub", _SYNC_CONVERGENCE_TIMEOUT_SECONDS, scrubbed
            )
            record_scrubbed = _record_for_agent(runtime, sync_e2e_account, agent_id)
            assert record_scrubbed is not None
            assert record_scrubbed.display_name == record_one.display_name, "Metadata must survive the scrub"
            # This hosting install keeps its key: the landing shows no unlock banner.
            _goto_landing(page, origin)
            assert page.query_selector("#sync-unlock-banner") is None, (
                "Clearing the password must not lock the device that holds the key"
            )

            # Setting P3 restores the bundle and re-pushes the pending secrets.
            _set_master_password_via_ui(page, origin, password_three)
            bundle_three = _wait_for_bundle(runtime, sync_e2e_account, _SYNC_CONVERGENCE_TIMEOUT_SECONDS)
            assert _unwrapped_dek(bundle_three, password_three) == dek
            _wait_for_synced_secrets(runtime, sync_e2e_account, agent_id, _SYNC_CONVERGENCE_TIMEOUT_SECONDS)

        # A fresh install (machine loss) unlocks with the final password.
        _wipe_local_install(runtime)
        with electron_app_session(runtime.template_path, find_free_port(), runtime.host_config_root) as (
            _browser,
            page,
        ):
            origin = _sign_in_headless(
                runtime, page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value()
            )
            _unlock_via_banner(page, origin, password_three)
            _assert_remote_row_visible(page, origin, agent_id)
    finally:
        _destroy_test_containers_best_effort(runtime)
