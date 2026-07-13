"""End-to-end workspace-sync release tests: real Electron, real connector, real backups.

Each test runs in the minds-snapshot offload sandbox (warm Electron /
Playwright / Xvfb / Docker toolchain) against a REAL deployed connector env
whose coordinates arrive via the ``MINDS_SYNC_E2E_*`` env vars -- forwarded
into the sandbox only on ``run_minds_release_tests`` CI runs (the
``sync_e2e_env`` fixture skips otherwise). Everything after per-test setup is
driven through the real Electron UI over Playwright/CDP: sign-in, workspace
association, backup configuration, the master-password settings panel, the
landing unlock banner, and the backup download link. Direct connector reads
(via the plugin client) are used only to *wait* for server-side convergence,
never to mutate.

Isolation model: every test gets its own minds root name
(``minds-ci-e2e<rand>``), so the app derives a private data root + mngr host
dir + docker container prefix under the pytest-faked ``$HOME`` -- tests never
touch the snapshot's baked ``minds-staging`` workspace and can destroy their
own installs freely. Accounts are per-test (``sync_e2e_account``) under the
env's seeded paid domain, so imbue-cloud backups (R2 provisioning) work.
"""

import json
import shutil
import subprocess
import threading
import time
import zipfile
from base64 import b64decode
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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from pydantic import AnyUrl
from pydantic import SecretStr
from test_snapshot_resume import _ensure_restic_on_sandbox_host
from test_snapshot_resume import _isolated_host_config_root

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.secret_wrapping import SecretWrappingError
from imbue.minds.bootstrap import minds_data_dir_for
from imbue.minds.bootstrap import mngr_prefix_for
from imbue.minds.desktop_client.dek_store import unwrap_bundle_json
from imbue.minds.desktop_client.e2e_workspace_runner import _agent_id_from_subdomain
from imbue.minds.desktop_client.e2e_workspace_runner import _backend_origin_from_page
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
from imbue.mngr_imbue_cloud.data_types import SyncKeyBundle
from imbue.mngr_imbue_cloud.data_types import SyncWorkspaceRecord
from imbue.mngr_imbue_cloud.errors import ImbueCloudError

_SENTINEL_FILENAME: Final[str] = "e2e-backup-sentinel.txt"
_DOCKER_STATE_MARKER: Final[str] = "docker-state"

# How long UI-observable convergence may take. The first imbue-cloud backup
# uploads the workspace host_dir to real R2, so its budget is the largest.
_SIGN_IN_TIMEOUT_SECONDS: Final[int] = 90
_ACCOUNT_VISIBLE_TIMEOUT_SECONDS: Final[int] = 120
_BACKUP_CONFIGURE_TIMEOUT_SECONDS: Final[int] = 420
_FIRST_BACKUP_TIMEOUT_SECONDS: Final[int] = 900
_SYNC_CONVERGENCE_TIMEOUT_SECONDS: Final[int] = 300
_UNLOCK_BANNER_TIMEOUT_SECONDS: Final[int] = 240
_DOWNLOAD_LINK_TIMEOUT_SECONDS: Final[int] = 300
# The sync scheduler reconciles every 60s; two full ticks with margin is
# enough to observe "the revision did NOT advance".
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
    """Poll ``probe`` (None = not yet) until it yields a value, or fail loudly."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        result = probe()
        if result is not None:
            return result
        threading.Event().wait(timeout=3.0)
    raise AssertionError(f"Timed out after {timeout_seconds}s waiting for {description}")


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
    """Drop the restore-verification sentinel into the workspace before its first backup."""
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-lc", f"cat > /code/{_SENTINEL_FILENAME}"],
        input=content,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"Could not write the sentinel: {result.stderr}"


def _wipe_local_install(runtime: _SyncE2ERuntime) -> None:
    """Simulate total machine loss: no minds data, no mngr host dir, no containers."""
    logger.info("Wiping local install: {} and containers with prefix {}", runtime.data_root, runtime.mngr_prefix)
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
    created_agent_ids: list[str] = []
    create_workspace_via_electron(
        runtime.template_path,
        workspace_name,
        find_free_port(),
        host_config_dir=runtime.host_config_root,
        on_workspace_ready=lambda page: created_agent_ids.append(_agent_id_from_subdomain(page.url)),
    )
    assert created_agent_ids, "The create flow finished without a workspace URL"
    logger.info("Created workspace {} -> {}", workspace_name, created_agent_ids[0])
    return created_agent_ids[0]


def _sign_in_via_ui(page: Page, email: str, password: str) -> str:
    """Sign in through the real /auth/login form; returns the backend origin."""
    origin = _backend_origin_from_page(page)
    page.goto(f"{origin}/auth/login", wait_until="domcontentloaded")
    page.wait_for_selector("#signin-email", state="visible", timeout=30_000)
    page.fill("#signin-email", email)
    page.fill("#signin-password", password)
    page.click("#signin-btn")

    def landed() -> bool:
        url = page.url
        return "/auth/" not in url and ("/post-login" not in url)

    deadline = time.monotonic() + _SIGN_IN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if landed():
            logger.info("Signed in as {}; landed at {}", email, page.url)
            return origin
        threading.Event().wait(timeout=1.0)
    raise AssertionError(
        f"Sign-in for {email} did not leave the auth flow within {_SIGN_IN_TIMEOUT_SECONDS}s ({page.url})"
    )


def _associate_workspace_via_ui(page: Page, origin: str, agent_id: str, email: str) -> None:
    """Associate the workspace with the signed-in account from its settings page."""
    settings_url = f"{origin}/workspace/{agent_id}/settings"

    def account_option_ready() -> bool | None:
        page.goto(settings_url, wait_until="domcontentloaded")
        if page.query_selector("#associate-form") is None:
            return None
        option_labels = page.eval_on_selector_all(
            '#associate-form select[name="user_id"] option', "els => els.map(e => e.textContent.trim())"
        )
        return True if email in option_labels else None

    _wait_until(f"the associate form to offer {email}", _ACCOUNT_VISIBLE_TIMEOUT_SECONDS, account_option_ready)
    page.select_option('#associate-form select[name="user_id"]', label=email)
    page.click('#associate-form button[type="submit"]')

    def associated() -> bool | None:
        page.goto(settings_url, wait_until="domcontentloaded")
        if page.query_selector("#associate-form") is not None:
            return None
        return True if email in page.inner_text("body") else None

    _wait_until(f"the settings page to show {email} as the account", 60, associated)
    logger.info("Associated {} with {}", agent_id, email)


def _configure_backups_via_ui(
    page: Page, origin: str, agent_id: str, provider: str, api_key_env: str | None = None
) -> None:
    """Configure backups through the workspace settings form and wait for provisioning."""
    page.goto(f"{origin}/workspace/{agent_id}/settings", wait_until="domcontentloaded")
    page.wait_for_selector("#backup-configure-toggle-btn", state="visible", timeout=30_000)
    page.click("#backup-configure-toggle-btn")
    page.wait_for_selector("#backup-provider-select", state="visible", timeout=10_000)
    page.select_option("#backup-provider-select", provider)
    if api_key_env is not None:
        page.wait_for_selector("#backup-api-key-row", state="visible", timeout=10_000)
        page.fill("#backup-api-key-env-input", api_key_env)
    page.click("#backup-configure-submit-btn")

    def provisioned() -> bool | None:
        error_text = page.inner_text("#backup-error") if page.query_selector("#backup-error") else ""
        if error_text.strip():
            raise AssertionError(f"Backup configuration surfaced an error: {error_text.strip()}")
        status = page.inner_text("#backup-status-line") if page.query_selector("#backup-status-line") else ""
        lowered = status.strip().lower()
        if lowered and "not configured" not in lowered and "loading" not in lowered:
            return True
        return None

    _wait_until(
        f"backup provisioning ({provider}) to finish for {agent_id}",
        _BACKUP_CONFIGURE_TIMEOUT_SECONDS,
        provisioned,
    )
    logger.info("Backups configured ({}) for {}", provider, agent_id)


def _set_master_password_via_ui(page: Page, origin: str, new_password: str) -> None:
    """Change (or clear, with an empty string) the master password on /settings."""
    page.goto(f"{origin}/settings", wait_until="domcontentloaded")
    page.wait_for_selector('[data-settings-nav="backups"]', state="visible", timeout=15_000)
    page.click('[data-settings-nav="backups"]')
    page.wait_for_selector("#backup-new-password", state="visible", timeout=10_000)
    page.fill("#backup-new-password", new_password)
    page.fill("#backup-new-password-confirm", new_password)
    page.click("#backup-change-password-btn")

    def change_reported() -> bool | None:
        error_text = page.inner_text("#backup-change-error") if page.query_selector("#backup-change-error") else ""
        if error_text.strip():
            raise AssertionError(f"Master password change surfaced an error: {error_text.strip()}")
        results = page.query_selector("#backup-change-results")
        if results is None:
            return None
        results_class = results.get_attribute("class") or ""
        if "hidden" in results_class.split():
            return None
        results_text = results.inner_text()
        assert "FAILED" not in results_text, f"Master password change reported a failure: {results_text}"
        return True

    _wait_until("the master password change to report success", 120, change_reported)
    logger.info("Master password {} via settings", "cleared" if new_password == "" else "updated")


def _landing_backup_badge_text(page: Page, agent_id: str) -> str | None:
    selector = f'[data-agent-id="{agent_id}"] .landing-backup-badge'
    if page.query_selector(selector) is None:
        return None
    return page.inner_text(selector).strip()


def _wait_for_backed_up_badge(page: Page, origin: str, agent_id: str) -> None:
    """Reload the landing page until this workspace's badge reports a completed backup."""

    def backed_up() -> bool | None:
        page.goto(f"{origin}/", wait_until="domcontentloaded")
        # Give the badge JS a beat to fetch per-workspace backup status.
        page.wait_for_timeout(2_000)
        badge = _landing_backup_badge_text(page, agent_id)
        return True if badge is not None and badge.startswith("Backed up") else None

    _wait_until(
        f"the landing badge to report a completed backup for {agent_id}", _FIRST_BACKUP_TIMEOUT_SECONDS, backed_up
    )
    logger.info("Landing badge reports a completed backup for {}", agent_id)


def _wait_for_unlock_banner(page: Page, origin: str) -> None:
    def banner_present() -> bool | None:
        page.goto(f"{origin}/", wait_until="domcontentloaded")
        return True if page.query_selector("#sync-unlock-banner") is not None else None

    _wait_until("the sync unlock banner to appear on the landing page", _UNLOCK_BANNER_TIMEOUT_SECONDS, banner_present)


def _unlock_via_banner(page: Page, origin: str, password: str, expect_success: bool = True) -> None:
    """Drive the landing unlock banner; asserts the expected outcome."""
    _wait_for_unlock_banner(page, origin)
    page.fill("#sync-unlock-password", password)
    page.click("#sync-unlock-btn")
    if expect_success:

        def banner_gone() -> bool | None:
            page.goto(f"{origin}/", wait_until="domcontentloaded")
            return True if page.query_selector("#sync-unlock-banner") is None else None

        _wait_until("the unlock banner to clear after unlocking", 60, banner_gone)
        logger.info("Unlocked synced workspaces via the banner")
    else:
        page.wait_for_selector("#sync-unlock-error:not(.hidden)", state="visible", timeout=30_000)
        logger.info("Wrong password was refused by the unlock banner, as expected")


def _assert_remote_row_visible(page: Page, origin: str, agent_id: str) -> None:
    """The workspace renders as a greyed other-device row with a remove control."""

    def remote_row() -> bool | None:
        page.goto(f"{origin}/", wait_until="domcontentloaded")
        card = page.query_selector(f'[data-agent-id="{agent_id}"]')
        if card is None:
            return None
        remove_button = card.query_selector("[data-remove-host-id]")
        return True if remove_button is not None else None

    _wait_until(f"a remote-device landing row for {agent_id}", 120, remote_row)


def _download_backup_zip(page: Page, origin: str, agent_id: str, dest_dir: Path) -> Path:
    """Click the landing row's download link and return the saved zip path.

    Prefers the real download event; falls back to an in-page fetch of the
    same product route (cookie-authenticated) if the Electron content view
    does not surface Playwright download events over CDP.
    """
    link_selector = f'[data-agent-id="{agent_id}"] .landing-backup-download'

    def link_visible() -> bool | None:
        page.goto(f"{origin}/", wait_until="domcontentloaded")
        page.wait_for_timeout(2_000)
        link = page.query_selector(link_selector)
        if link is None:
            return None
        link_class = link.get_attribute("class") or ""
        return True if "hidden" not in link_class.split() else None

    _wait_until(f"the backup download link for {agent_id}", _DOWNLOAD_LINK_TIMEOUT_SECONDS, link_visible)

    zip_path = dest_dir / f"{agent_id}-backup.zip"
    try:
        with page.expect_download(timeout=180_000) as download_info:
            page.click(link_selector)
        download_info.value.save_as(zip_path)
        logger.info("Downloaded backup zip via the UI download event: {}", zip_path)
        return zip_path
    except (PlaywrightError, PlaywrightTimeoutError) as e:
        logger.warning("No Playwright download event ({}); falling back to an in-page fetch of the export route", e)
    result = page.evaluate(
        """(aid) => fetch('/api/v1/workspaces/' + aid + '/backups/latest/export', {method: 'POST'})
            .then((resp) => {
                if (!resp.ok) {
                    return resp.text().then((body) => ({ok: false, status: resp.status, body: body.slice(0, 500)}));
                }
                return resp.arrayBuffer().then((buf) => {
                    const bytes = new Uint8Array(buf);
                    let binary = '';
                    const chunk = 0x8000;
                    for (let i = 0; i < bytes.length; i += chunk) {
                        binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
                    }
                    return {ok: true, b64: btoa(binary)};
                });
            })""",
        agent_id,
    )
    assert result.get("ok"), f"Backup export failed: {result}"
    zip_path.write_bytes(b64decode(result["b64"]))
    logger.info("Downloaded backup zip via the export route fallback: {}", zip_path)
    return zip_path


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
@pytest.mark.timeout(2400)
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
    remote-row download link -- verifying a sentinel file round-tripped
    byte-for-byte through R2.
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
            origin = _sign_in_via_ui(page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value())
            _associate_workspace_via_ui(page, origin, agent_id, sync_e2e_account.email)
            _configure_backups_via_ui(page, origin, agent_id, "IMBUE_CLOUD")
            _set_master_password_via_ui(page, origin, master_password)
            _wait_for_backed_up_badge(page, origin, agent_id)

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
            origin = _sign_in_via_ui(page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value())
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
@pytest.mark.timeout(1800)
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
            origin = _sign_in_via_ui(page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value())

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

            # The workspace shows as associated in the real settings UI.
            page.goto(f"{origin}/workspace/{agent_id}/settings", wait_until="domcontentloaded")
            assert page.query_selector("#associate-form") is None, "The associate form should be gone post-migration"
            assert sync_e2e_account.email in page.inner_text("body")

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
            origin = _sign_in_via_ui(page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value())
            _unlock_via_banner(page, origin, legacy_password)
            _assert_remote_row_visible(page, origin, agent_id)
    finally:
        _destroy_test_containers_best_effort(runtime)


@pytest.mark.minds_snapshot_resume
@pytest.mark.docker
@pytest.mark.rsync
@pytest.mark.timeout(1800)
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
            origin = _sign_in_via_ui(page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value())
            _associate_workspace_via_ui(page, origin, agent_id, sync_e2e_account.email)
            _configure_backups_via_ui(
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
            page.goto(f"{origin}/", wait_until="domcontentloaded")
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
            origin = _sign_in_via_ui(page, sync_e2e_account.email, sync_e2e_account.password.get_secret_value())
            _unlock_via_banner(page, origin, password_three)
            _assert_remote_row_visible(page, origin, agent_id)
    finally:
        _destroy_test_containers_best_effort(runtime)
