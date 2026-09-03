"""Account sign-in plumbing for the minds desktop client.

Sign-up/sign-in itself happens on the connector's hosted accounts pages in
the system browser: the desktop client just launches ``mngr imbue_cloud auth
login`` (which opens the browser and receives the session via a localhost
loopback + PKCE code exchange) and mirrors the resulting account into its own
state -- default-account selection, ``[providers.imbue_cloud_<slug>]``
registration, and the observer bounce. The Mithril UI drives the flow through
the small JSON surface here (`/auth/api/web-login/*`) and renders the
waiting/copy-link modal itself.

The legacy server-rendered auth pages (sign-up/sign-in forms, the check-email
poll, the sign-in modal) are gone with the rest of the JinjaX surface.
"""

import json
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Final
from urllib.parse import urlencode

from flask import Blueprint
from flask import Response
from flask import request
from loguru import logger

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.bootstrap import MindsRoot
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthFailedCliError
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudAuthSession
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.responses import make_redirect_response
from imbue.minds.desktop_client.responses import make_response
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.mngr_settings.imbue_cloud_accounts import set_imbue_cloud_provider_for_account
from imbue.minds.mngr_settings.imbue_cloud_accounts import unset_imbue_cloud_provider_for_account
from imbue.minds.primitives import OutputFormat
from imbue.minds.utils.output import emit_event
from imbue.mngr_latchkey.core import LatchkeyError
from imbue.mngr_latchkey.forward_supervisor import LatchkeyForwardSupervisor

# Shown when a login fails for a reason the connector never got to judge
# (subprocess crash, connector unreachable, malformed response). ``str(exc)``
# for those is the traceback-free "auth login failed (exit 1); see the desktop
# client logs for details" -- the right thing in a log, unusable in the UI --
# and the full detail is already logged by ``_expect_success``.
_UNAVAILABLE_AUTH_SERVICE_MESSAGE: Final[str] = (
    "We could not reach the Imbue sign-in service. Check your internet connection and try again."
)


def _user_facing_auth_message(exc: ImbueCloudCliError) -> str:
    """Return copy safe to render in the UI for a failed plugin auth call."""
    if isinstance(exc, ImbueCloudAuthFailedCliError):
        return exc.auth_message
    logger.warning("Auth call failed without a structured connector verdict: {}", exc)
    return _UNAVAILABLE_AUTH_SERVICE_MESSAGE


def _json_response(data: dict[str, object], status_code: int = 200) -> Response:
    return make_response(
        content=json.dumps(data),
        media_type="application/json",
        status_code=status_code,
    )


def _get_session_store() -> MultiAccountSessionStore:
    session_store = get_state().session_store
    assert session_store is not None, "create_desktop_client() was constructed without a session_store"
    return session_store


def _get_output_format() -> OutputFormat:
    return get_state().auth_output_format


def _get_connector_url() -> str:
    """Read the connector URL out of the loaded client env config.

    The desktop client always populates ``client_env_config`` from a
    ``--config-file`` (or the build-time default), so this assert fires only
    in tests that forgot to wire one up.
    """
    client_env_config = get_state().client_env_config
    assert client_env_config is not None, "create_desktop_client() was constructed without a client_env_config"
    return str(client_env_config.connector_url).rstrip("/")


def _bounce_forward_observe() -> None:
    """Bounce the single discovery observer so a freshly-written provider entry
    takes effect within the same minds session.

    Sends ``SIGHUP`` to the detached ``mngr latchkey forward`` supervisor via
    ``LatchkeyForwardSupervisor.bounce()``, restarting only its ``mngr observe``
    child (the shared gateway, reverse tunnels, and per-agent state stay up). Its
    next snapshot is written to the shared discovery log that minds' ``mngr forward
    --observe-via-file`` tails, so no separate ``mngr forward`` bounce is needed.
    """
    bounce_latchkey_forward_supervisor(get_state().latchkey_forward_supervisor)


def bounce_latchkey_forward_supervisor(supervisor: LatchkeyForwardSupervisor | None) -> None:
    """Bounce the detached ``mngr latchkey forward`` supervisor's observe child.

    No-op when no supervisor handle is available (e.g. tests). ``bounce()``
    starts the supervisor if none is currently running.
    """
    if supervisor is None:
        return
    try:
        supervisor.bounce()
    except (OSError, RuntimeError, LatchkeyError) as e:
        logger.warning("Failed to bounce mngr latchkey forward: {}", e)


def signout_user_via_plugin(user_id: str) -> None:
    """Sign ``user_id`` out via the mngr_imbue_cloud plugin and clear local state.

    Resolves the email for ``user_id`` against the cached ``auth list``,
    runs ``mngr imbue_cloud auth signout --account <email>`` (plugin owns
    the SuperTokens session; only this device's session is revoked),
    invalidates the local identity cache, and tears down the matching
    ``[providers.imbue_cloud_<slug>]`` block / bounces ``mngr observe`` so
    ``mngr create``/``list`` reflect the new state immediately.

    No-ops gracefully when the user isn't currently visible to the
    plugin -- the cache is still invalidated so a stale entry can't
    survive.
    """
    session_store = _get_session_store()
    cli = get_state().imbue_cloud_cli
    session = session_store.get_session(user_id)
    signed_out_email: str | None = None
    if session is None:
        logger.warning("No mirrored account for user {}; skipping plugin signout", user_id[:8])
    elif cli is None:
        logger.warning("imbue_cloud_cli is not configured; skipping plugin signout for user {}", user_id[:8])
    else:
        signed_out_email = str(session.email)
        try:
            cli.auth_signout(signed_out_email)
        except ImbueCloudCliError as exc:
            logger.warning("`mngr imbue_cloud auth signout` failed for {}: {}", signed_out_email, exc)
    session_store.invalidate_identity_cache()
    _kick_sync_scheduler()
    wake_ui_state_publisher()
    if signed_out_email and unset_imbue_cloud_provider_for_account(
        signed_out_email, root=MindsRoot.from_environment()
    ):
        _bounce_forward_observe()


def _kick_sync_scheduler() -> None:
    """Request an immediate workspace-record sync pass after an auth change."""
    scheduler = get_state().sync_scheduler
    if scheduler is not None:
        scheduler.kick()


def wake_ui_state_publisher() -> None:
    """Make every open window re-read the account state after an auth change.

    The ``/ui/ws`` channel carries the signed-in identity on its ``accounts``
    frames, but the publisher only re-derives when a producer signals a
    change. Auth changes originate on a Flask request thread and move nothing
    the resolver watches, so without this nudge the home screen's account
    launcher would keep the signed-out account's label indefinitely.
    """
    publisher = get_state().ui_publisher
    if publisher is not None:
        publisher.notify_change()


# ---------------------------------------------------------------------------
# The web-login flow: launch the plugin's browser login and track it.
#
# Each in-progress flow is tracked by a server-generated key the frontend
# polls so it can render the "waiting for the browser" modal (with the
# copy-the-link fallback) without blocking on the subprocess.
# ---------------------------------------------------------------------------

# Kept above the desktop's web-login subprocess kill deadline
# (imbue_cloud_cli._WEB_LOGIN_TIMEOUT_SECONDS) so a slow-but-valid browser
# sign-in never has its flow status swept out from under the polling frontend as
# a spurious "sign-in flow expired".
_WEB_LOGIN_FLOW_TTL_SECONDS = 11 * 60

# Passed to the plugin's login subcommand so its browser success page bounces
# straight back to the desktop app: a bare minds:// deeplink focuses the app
# without navigating (see the Electron main process's handleDeeplink).
_MINDS_FOCUS_DEEPLINK = "minds://"


class _WebLoginFlowStatus(FrozenModel):
    """Status snapshot for a single in-flight browser-login subprocess.

    ``state`` is one of ``"running"``, ``"finishing"``, ``"done"``, or
    ``"error"``. ``"finishing"`` means the sign-in was written to disk but the
    desktop client is still mirroring it (registering the provider, bouncing
    the latchkey-forward supervisor); the frontend brings the app to the front
    and shows "Finishing up..." during it, then refreshes once ``"done"``.
    ``login_url_file`` is where the plugin writes the sign-in URL once its
    loopback listener is live; the status endpoint reads it lazily for the
    copy-the-link fallback while the flow is running. Once the subprocess
    exits the temp file is deleted and the URL (dead, but still rendered by
    the modal) is carried in ``login_url`` instead.
    """

    state: str
    login_url_file: str | None = None
    login_url: str | None = None
    user_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    error: str | None = None
    deadline: float | None = None


_web_login_flows: dict[str, _WebLoginFlowStatus] = {}
_web_login_flows_lock = threading.Lock()


def _record_web_login_status(flow_id: str, status: _WebLoginFlowStatus) -> None:
    with _web_login_flows_lock:
        _prune_expired_web_login_flows_locked()
        _web_login_flows[flow_id] = status


def _read_web_login_status(flow_id: str) -> _WebLoginFlowStatus | None:
    with _web_login_flows_lock:
        _prune_expired_web_login_flows_locked()
        return _web_login_flows.get(flow_id)


def _prune_expired_web_login_flows_locked() -> None:
    now = time.monotonic()
    expired = [flow_id for flow_id, st in _web_login_flows.items() if st.deadline is not None and st.deadline <= now]
    for flow_id in expired:
        _web_login_flows.pop(flow_id, None)


def _flow_deadline() -> float:
    return time.monotonic() + _WEB_LOGIN_FLOW_TTL_SECONDS


def _run_web_login_subprocess(
    flow_id: str,
    url_file: Path,
    imbue_cloud_cli: ImbueCloudCli,
    session_store: MultiAccountSessionStore,
    sync_scheduler: WorkspaceSyncScheduler | None,
    minds_config: MindsConfig | None,
    output_format: OutputFormat,
    latchkey_forward_supervisor: LatchkeyForwardSupervisor | None,
    connector_url: str,
) -> None:
    """Run ``mngr imbue_cloud auth login`` in a background thread.

    The plugin opens the system browser onto the hosted accounts page,
    listens on its own localhost port for the one-time code, exchanges it,
    and writes the session to its own state directory. We then mirror the
    resulting account identity into ``MultiAccountSessionStore`` so the
    desktop UI can render it, register a ``[providers.imbue_cloud_<slug>]``
    entry (force-enabled, even if the user previously clicked Disable on it
    in the providers panel), and bounce the detached ``mngr latchkey
    forward`` supervisor (the single discovery observer) so the new provider
    config is picked up immediately.
    """
    try:
        try:
            result = imbue_cloud_cli.auth_login(success_redirect_url=_MINDS_FOCUS_DEEPLINK, url_file=url_file)
        finally:
            # The URL is dead once the subprocess exits (its loopback listener
            # is gone): capture it for the final status records and delete the
            # temp file so login attempts do not accumulate files.
            login_url = _consume_login_url_file(url_file)
    except ImbueCloudCliError as exc:
        logger.warning("Plugin web-login subprocess failed: {}", exc)
        _record_web_login_status(
            flow_id,
            _WebLoginFlowStatus(
                state="error",
                login_url=login_url,
                # The frontend renders this verbatim in the modal's error box.
                error=_user_facing_auth_message(exc),
                deadline=_flow_deadline(),
            ),
        )
        return

    # The signin itself is complete at this point (the plugin subprocess wrote
    # the session to disk), so mark the flow "finishing": the frontend brings
    # the app to the front and shows "Finishing up..." while we mirror the
    # session below, rather than leaving the user on the "waiting for the
    # browser" state until the provider registration + supervisor bounce finish.
    _record_web_login_status(
        flow_id,
        _WebLoginFlowStatus(
            state="finishing",
            login_url=login_url,
            user_id=str(result.user_id),
            email=str(result.email),
            display_name=result.display_name,
            deadline=_flow_deadline(),
        ),
    )

    # Anything that goes wrong while mirroring the signin into the desktop
    # client must still resolve the flow status -- the frontend polls it, so an
    # unresolved crash here would leave the user stuck. Nothing is caught: a
    # mirroring crash propagates to the CG's ObservableThread (which logs it),
    # while the finally block flips a still-unresolved status to "error".
    try:
        _mirror_signin_result(
            result=result,
            session_store=session_store,
            sync_scheduler=sync_scheduler,
            minds_config=minds_config,
            output_format=output_format,
            latchkey_forward_supervisor=latchkey_forward_supervisor,
            connector_url=connector_url,
        )
        _record_web_login_status(
            flow_id,
            _WebLoginFlowStatus(
                state="done",
                login_url=login_url,
                user_id=str(result.user_id),
                email=str(result.email),
                display_name=result.display_name,
                deadline=_flow_deadline(),
            ),
        )
    finally:
        latest_status = _read_web_login_status(flow_id)
        if latest_status is not None and latest_status.state in ("running", "finishing"):
            _record_web_login_status(
                flow_id,
                _WebLoginFlowStatus(
                    state="error",
                    login_url=login_url,
                    error=(
                        f"Signed in as {result.email}, but applying the signin locally failed; "
                        "see the desktop client logs for details."
                    ),
                    deadline=_flow_deadline(),
                ),
            )


def _mirror_signin_result(
    result: ImbueCloudAuthSession,
    session_store: MultiAccountSessionStore,
    sync_scheduler: WorkspaceSyncScheduler | None,
    minds_config: MindsConfig | None,
    output_format: OutputFormat,
    latchkey_forward_supervisor: LatchkeyForwardSupervisor | None,
    connector_url: str,
) -> None:
    """Mirror a completed plugin signin into the desktop client.

    Runs on the web-login background thread, so every dependency is passed in
    explicitly -- there is no Flask app context to resolve state from.
    """
    session_store.invalidate_identity_cache()
    if sync_scheduler is not None:
        sync_scheduler.note_account_signin(str(result.user_id), str(result.email))
    if minds_config is not None and minds_config.get_default_account_id() is None:
        minds_config.set_default_account_id(str(result.user_id))

    # Explicit signin -- always re-enable the provider entry, even if the
    # user previously clicked Disable on it in the providers panel.
    if set_imbue_cloud_provider_for_account(
        str(result.email),
        connector_url=connector_url,
        root=MindsRoot.from_environment(),
        force_enable=True,
    ):
        bounce_latchkey_forward_supervisor(latchkey_forward_supervisor)

    emit_event(
        "auth_success",
        {
            "message": f"Signed in as {result.display_name or result.email}",
            "email": str(result.email),
        },
        output_format,
    )


def _handle_web_login_start() -> Response:
    """Kick off the plugin's browser login in a background thread (POST).

    Returns immediately with a flow id the frontend polls. The plugin
    subprocess opens the system browser, captures the loopback callback, and
    writes the session itself; the background thread then mirrors the account
    identity into ``MultiAccountSessionStore`` once the subprocess finishes.
    """
    state = get_state()
    imbue_cloud_cli: ImbueCloudCli | None = state.imbue_cloud_cli
    session_store = _get_session_store()
    output_format = _get_output_format()
    minds_config: MindsConfig | None = state.minds_config
    latchkey_forward_supervisor: LatchkeyForwardSupervisor | None = state.latchkey_forward_supervisor
    sync_scheduler: WorkspaceSyncScheduler | None = state.sync_scheduler
    root_cg: ConcurrencyGroup | None = state.root_concurrency_group
    if imbue_cloud_cli is None:
        return _json_response({"status": "ERROR", "error": "imbue_cloud_cli is not configured"}, 503)
    if root_cg is None:
        return _json_response({"status": "ERROR", "error": "root_concurrency_group is not configured"}, 503)

    flow_id = secrets.token_urlsafe(16)
    url_file = Path(tempfile.gettempdir()) / f"minds-web-login-{flow_id}.url"
    _record_web_login_status(
        flow_id,
        _WebLoginFlowStatus(state="running", login_url_file=str(url_file), deadline=_flow_deadline()),
    )
    root_cg.start_new_thread(
        target=_run_web_login_subprocess,
        kwargs={
            "flow_id": flow_id,
            "url_file": url_file,
            "imbue_cloud_cli": imbue_cloud_cli,
            "session_store": session_store,
            "sync_scheduler": sync_scheduler,
            "minds_config": minds_config,
            "output_format": output_format,
            "latchkey_forward_supervisor": latchkey_forward_supervisor,
            "connector_url": _get_connector_url(),
        },
        name="imbue-cloud-web-login",
        is_checked=False,
    )
    return _json_response({"status": "OK", "flow_id": flow_id})


def _consume_login_url_file(url_file: Path) -> str | None:
    """Read the plugin's sign-in URL (best effort) and delete the temp file."""
    try:
        raw = url_file.read_text().strip()
    except OSError:
        raw = ""
    url_file.unlink(missing_ok=True)
    return raw or None


def _read_login_url(status: _WebLoginFlowStatus) -> str | None:
    """The sign-in URL for the copy-the-link fallback, once the plugin has written it."""
    if status.login_url is not None:
        return status.login_url
    if status.login_url_file is None:
        return None
    url_path = Path(status.login_url_file)
    try:
        raw = url_path.read_text().strip()
    except OSError:
        return None
    return raw or None


def _handle_web_login_status(flow_id: str) -> Response:
    """Poll-friendly status for an in-flight web login (GET).

    The frontend polls this until ``state`` is ``"done"`` or ``"error"``;
    ``login_url`` appears as soon as the plugin's loopback listener is live.
    """
    status = _read_web_login_status(flow_id)
    if status is None:
        return _json_response({"status": "ERROR", "error": "Unknown flow id"}, 404)
    return _json_response(
        {
            "status": "OK",
            "state": status.state,
            "login_url": _read_login_url(status),
            "user_id": status.user_id,
            "email": status.email,
            "display_name": status.display_name,
            "error": status.error,
        }
    )


def _handle_legacy_auth_page_redirect() -> Response:
    """Redirect the retired /auth/login and /auth/signup page URLs into the SPA.

    Sign-in now happens on the hosted browser page; ``?web-login=1`` makes the
    SPA start that flow on load. Kept because legacy surfaces (older static
    JS, stale bookmarks) still navigate here.
    """
    params: dict[str, str] = {"web-login": "1"}
    message = request.args.get("message")
    if message:
        params["web-login-message"] = message
    return make_redirect_response(f"/?{urlencode(params)}", status_code=302)


def _handle_reset_password_redirect() -> Response:
    """Redirect legacy in-app reset links to the connector's reset page.

    The reset link embedded in current reset emails points at the connector
    directly (its ``/auth/reset-password`` page); this redirect keeps any
    older in-app links working.
    """
    token = request.args.get("token", "")
    target = _get_connector_url() + "/auth/reset-password"
    if token:
        target = f"{target}?{urlencode({'token': token})}"
    return make_redirect_response(target, status_code=302)


def create_supertokens_blueprint() -> Blueprint:
    """Create a Flask blueprint with the account-auth routes (mounted under /auth)."""
    blueprint = Blueprint("supertokens", __name__, url_prefix="/auth")

    blueprint.add_url_rule("/api/web-login/start", view_func=_handle_web_login_start, methods=["POST"])
    blueprint.add_url_rule("/api/web-login/status/<flow_id>", view_func=_handle_web_login_status, methods=["GET"])
    blueprint.add_url_rule("/login", view_func=_handle_legacy_auth_page_redirect, methods=["GET"], endpoint="login")
    blueprint.add_url_rule("/signup", view_func=_handle_legacy_auth_page_redirect, methods=["GET"], endpoint="signup")
    blueprint.add_url_rule("/reset-password", view_func=_handle_reset_password_redirect, methods=["GET"])

    return blueprint
