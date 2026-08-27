"""Typed wrapper around the ``mngr imbue_cloud …`` CLI surface.

Every operation that minds previously did via direct HTTP calls into the
``remote_service_connector`` (auth, host pool, LiteLLM keys, workspace
shares) now runs as an invocation of ``mngr imbue_cloud …`` handed to a
:class:`~imbue.minds.utils.mngr_caller.MngrCaller`, which runs it in a
pre-warmed, single-use ``mngr`` process. This avoids re-paying the
multi-second interpreter + plugin-import startup on every call.

The plugin always emits a JSON document on stdout for the success case and a
JSON ``{"error": ...}`` document on stderr for the failure case (see
``libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/_common.py``); this module
parses those into typed pydantic objects.
"""

import json as _json
import os
import tempfile
import threading
import time
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import PrivateAttr
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import MindError
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.minds.utils.mngr_caller import get_default_mngr_caller
from imbue.mngr_imbue_cloud.errors import CLIENT_TOO_OLD_FALLBACK_MESSAGE
from imbue.mngr_imbue_cloud.wire import WireModel

_DEFAULT_TIMEOUT_SECONDS = 60.0
_LEASE_TIMEOUT_SECONDS = 300.0
# The plugin's `auth login` enforces its own 300s listen deadline
# (_LOGIN_LISTEN_TIMEOUT_SECONDS in the plugin's cli/auth.py) with a proper
# timeout error; this outer kill deadline needs headroom over it (spawn, code
# exchange, session persist) so the plugin's message is the one that surfaces.
_WEB_LOGIN_TIMEOUT_SECONDS = 330.0
_KEY_OP_TIMEOUT_SECONDS = 90.0
# Force-destroy empties the bucket over S3 before deleting it, so it can run
# far longer than the other bucket ops (many objects, plus credential
# propagation waits).
_BUCKET_DESTROY_TIMEOUT_SECONDS = 600.0

# Env var consumed by the imbue_cloud plugin's CLI + provider config to
# discover the connector URL. Mirrored in libs/mngr_imbue_cloud/.../config.py;
# kept duplicated here to avoid pulling the plugin's config module into the
# desktop client.
_CONNECTOR_URL_SUBPROCESS_ENV: str = "MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL"
_ACCOUNTS_URL_SUBPROCESS_ENV: str = "MNGR__PROVIDERS__IMBUE_CLOUD__ACCOUNTS_URL"

# The plugin's error_class marker for a structured quota refusal, as written
# into its JSON stderr body by handle_imbue_cloud_errors. Substring-matched
# (like the 503 unavailable_signal) because log lines may surround the body.
_QUOTA_ERROR_CLASS_SIGNAL = "ImbueCloudQuotaExceededError"

# The plugin's error_class marker for a structured email-verification refusal
# (``code: email_not_verified``), written by handle_imbue_cloud_errors.
# Substring-matched like the quota signal.
_EMAIL_NOT_VERIFIED_ERROR_CLASS_SIGNAL = "ImbueCloudEmailNotVerifiedError"

# The plugin's error_class marker for the connector's structured HTTP 426
# "client too old" refusal, written by handle_imbue_cloud_errors. Substring-
# matched like the quota signal.
_CLIENT_TOO_OLD_ERROR_CLASS_SIGNAL = "ImbueCloudClientTooOldError"

# The connector's structured code for refusing to hard-delete the record of a
# workspace that still holds its pool lease (a 409 relayed by the plugin's
# ``sync records delete``). Substring-matched like the quota signal: the code
# rides inside the relayed connector body.
_LEASE_ACTIVE_CODE_SIGNAL = "lease_active"

# The plugin's error_class marker for a structured auth rejection, written by
# ``_persist_auth_response`` in the plugin's auth CLI whenever the connector
# answers an auth call with a non-OK status. Matched on the parsed body's
# ``error_class`` field rather than as a substring, because the accompanying
# ``status`` has to be read out of that body anyway.
_AUTH_FAILED_ERROR_CLASS = "AuthFailed"


class ImbueCloudCliError(MindError):
    """Raised when a `mngr imbue_cloud ...` invocation returns a non-zero exit code.

    The plugin emits structured JSON on both stdout (success) and stderr
    (failure), so we keep both around for debugging. They are populated by
    the helper that raises this class; default to empty strings so callers
    that only want the message can use the regular MindError signature.
    """

    exit_code: int = 1
    stdout: str = ""
    stderr: str = ""


class ImbueCloudUnavailableError(ImbueCloudCliError):
    """Subclass of CliError indicating the connector returned 503 (no matching pool host)."""


class ImbueCloudQuotaExceededCliError(ImbueCloudCliError):
    """Subclass of CliError indicating the connector refused the operation on a quota entitlement.

    A quota refusal is deterministic -- retrying the same call cannot
    succeed -- so callers (e.g. the backup-provisioning retry loop) treat it
    as terminal and surface it immediately instead of burning their retry
    budget.
    """


class ImbueCloudEmailNotVerifiedCliError(ImbueCloudCliError):
    """The connector refused the operation because the account's email is unverified.

    Raised for the connector's structured ``email_not_verified`` 403 (relayed
    through the plugin). Deterministic like a quota refusal -- retrying cannot
    succeed until the user clicks the verification link -- so callers surface
    a contextual "verify your email" prompt instead of a generic failure.
    ``email`` is the address the verification link goes to (None when the
    connector could not resolve one).
    """

    email: str | None = None


class ImbueCloudClientTooOldCliError(ImbueCloudCliError):
    """The connector refused the operation because this app version is no longer supported.

    Deterministic -- retrying cannot succeed until the app updates -- so
    callers surface an "update the app" prompt instead of a generic failure.
    """


class ImbueCloudAuthFailedCliError(ImbueCloudCliError):
    """The auth backend rejected an ``auth signin`` / ``signup`` / ``login`` attempt.

    ``auth_status`` carries the connector's own verdict (``WRONG_CREDENTIALS``,
    ``EMAIL_ALREADY_EXISTS``, ``FIELD_ERROR``, ...) and ``auth_message`` its
    user-facing explanation. Keeping both typed is what lets the sign-in UI
    render real copy: without this subclass every rejection collapses into the
    deliberately traceback-free "<command> failed (exit N)" fallback, which is
    right for a log line and useless in a sign-in form.
    """

    auth_status: str = "ERROR"
    auth_message: str = ""


class ImbueCloudLeaseActiveCliError(ImbueCloudCliError):
    """The connector refused to hard-delete a record because its workspace still holds a pool lease.

    Tombstone-first: destroying the workspace is what releases the lease (and
    retires the record), so the remedy is destroy, not remove-from-list.
    Deterministic -- retrying cannot succeed while the lease exists.
    """


class ImbueCloudSyncConflictCliError(ImbueCloudCliError):
    """A record push hit a 409 (revision CAS or active-agent conflict).

    ``stored_record`` carries the server's current row when the conflict was a
    revision CAS failure, so the caller can rebase and retry; None otherwise.
    """

    stored_record: dict[str, Any] | None = None


class ImbueCloudAuthSession(WireModel):
    """Result of a successful auth signin/signup/login invocation."""

    user_id: str
    email: str
    display_name: str | None = None
    needs_email_verification: bool = False


class ImbueCloudAuthAccount(WireModel):
    """One entry from `mngr imbue_cloud auth list`."""

    user_id: str
    email: str
    display_name: str | None = None
    is_active: bool = False


class LeasedHost(WireModel):
    """One row of `mngr imbue_cloud hosts list`."""

    host_db_id: str
    host_id: str
    agent_id: str
    vps_address: str
    ssh_user: str
    ssh_port: int
    container_ssh_port: int
    attributes: dict[str, Any] = Field(default_factory=dict)
    leased_at: str


class LiteLLMKeyMaterial(WireModel):
    """Result of `mngr imbue_cloud keys litellm create`."""

    key: SecretStr
    base_url: AnyUrl


class ShareCliRelayEndpoint(WireModel):
    """One relay a shared workspace tunnels to (from `shares create` / `shares status`)."""

    relay_id: str
    endpoint: str


class ShareCliRelayLogin(WireModel):
    """One relay's last tunnel Login stamp for a share (from `shares status`)."""

    relay_id: str
    last_login_at: str | None = None


class ShareCliInfo(WireModel):
    """Result of `mngr imbue_cloud shares create` / `shares status`."""

    host_id: str
    workspace_domain: str
    region: str
    state: str
    relay_endpoints: tuple[ShareCliRelayEndpoint, ...] = ()
    # Per-relay tunnel login stamps; `shares status` output only (ops signal,
    # not shown in the end-user UI).
    relays: tuple[ShareCliRelayLogin, ...] = ()
    relay_token: SecretStr | None = None
    last_tunnel_login_at: str | None = None
    cert_not_after: str | None = None


# How long a readiness poll may reuse a cached connector share lookup. The
# share's domain is immutable for its lifetime and the progress stamps riding
# along (cert expiry, tunnel login) change on the scale of the ACME/tunnel
# bring-up, so one connector read per window is plenty -- while the poll
# itself fires every ~2 seconds and each uncached read is a multi-second
# ``mngr imbue_cloud shares status`` subprocess.
_ACTIVE_SHARE_CACHE_TTL_SECONDS: Final[float] = 20.0


class CachedShareLookup(FrozenModel):
    """One cached connector share lookup (``share`` is None for 'not actively shared')."""

    share: ShareCliInfo | None = Field(description="The active share, or None when the host has no active share")


class ActiveShareCache(MutableModel):
    """Short-TTL cache of connector share lookups, keyed by host id.

    Serves the readiness poll: the poll needs the share's (immutable) domain
    plus slow-moving progress stamps every ~2 seconds, and an uncached lookup
    costs a multi-second CLI subprocess. Enable/disable invalidate their
    host's entry so state flips are observed immediately rather than at TTL
    expiry.
    """

    ttl_seconds: float = Field(
        default=_ACTIVE_SHARE_CACHE_TTL_SECONDS,
        frozen=True,
        description="How long one lookup may be reused",
    )
    _lookup_and_deadline_by_host_id: dict[str, tuple[float, CachedShareLookup]] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def get(self, host_id: str) -> CachedShareLookup | None:
        """The unexpired cached lookup for ``host_id``, or None on a miss."""
        with self._lock:
            entry = self._lookup_and_deadline_by_host_id.get(host_id)
            if entry is None:
                return None
            deadline, lookup = entry
            if time.monotonic() >= deadline:
                del self._lookup_and_deadline_by_host_id[host_id]
                return None
            return lookup

    def put(self, host_id: str, share: ShareCliInfo | None) -> None:
        with self._lock:
            self._lookup_and_deadline_by_host_id[host_id] = (
                time.monotonic() + self.ttl_seconds,
                CachedShareLookup(share=share),
            )

    def invalidate(self, host_id: str) -> None:
        with self._lock:
            self._lookup_and_deadline_by_host_id.pop(host_id, None)


class R2BucketKeyMaterial(WireModel):
    """A bucket-scoped S3 credential, as emitted by `mngr imbue_cloud bucket ...`.

    Mirror of the plugin's ``R2KeyMaterial`` JSON shape; the secret is
    revealed once at creation and never persisted by the connector.
    """

    access_key_id: str
    secret_access_key: SecretStr
    s3_endpoint: AnyUrl
    bucket_name: str
    access: str


class R2BucketInfo(WireModel):
    """Metadata for an R2 bucket, as emitted by `mngr imbue_cloud bucket info`."""

    bucket_name: str
    s3_endpoint: AnyUrl


class R2BucketCreateResult(WireModel):
    """Result of `mngr imbue_cloud bucket create`: the bucket plus its default key."""

    bucket: R2BucketInfo
    key: R2BucketKeyMaterial


class ImbueCloudCli(MutableModel):
    """Run ``mngr imbue_cloud …`` subcommands via a :class:`MngrCaller`.

    All invocations are routed through the shared ``MngrCaller``, which runs each
    one in a pre-warmed, single-use ``mngr`` process so repeated calls don't
    re-pay the interpreter + plugin-import startup cost.
    """

    mngr_caller: MngrCaller = Field(
        default_factory=get_default_mngr_caller,
        description=(
            "Runs each `mngr imbue_cloud …` invocation in a pre-warmed process. Defaults to the "
            "process-wide shared caller (initialized at startup) so imbue_cloud calls reuse the same "
            "warm-process machinery as the rest of the app."
        ),
    )
    connector_url: AnyUrl = Field(
        frozen=True,
        description=(
            "Base URL of the `remote_service_connector` for this environment. Passed into every "
            "`mngr imbue_cloud …` subprocess via the MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL "
            "env var; the plugin has no baked-in default."
        ),
    )
    accounts_base_url: AnyUrl | None = Field(
        default=None,
        frozen=True,
        description=(
            "Base URL of the tier's browser accounts origin (client.toml `accounts_base_url`, "
            "e.g. https://accounts.imbue.com on production). Passed to the plugin via the "
            "MNGR__PROVIDERS__IMBUE_CLOUD__ACCOUNTS_URL env var so `auth login` opens the hosted "
            "login page on the origin where Google OAuth and session cookies actually work. None "
            "on tiers without a dedicated accounts domain (the connector host serves the pages)."
        ),
    )

    def _run(
        self,
        args: Sequence[str],
        *,
        cg_name: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> MngrCallResult:
        argv = ["imbue_cloud", *args]
        # Layer the connector URL onto the warm process's inherited env so the
        # `mngr imbue_cloud` plugin reaches the right backend without a
        # baked-in default. The warm process already inherits HOME / PATH /
        # MNGR_HOST_DIR etc. from the minds backend, so only this override is
        # needed.
        env_overrides = {_CONNECTOR_URL_SUBPROCESS_ENV: str(self.connector_url).rstrip("/")}
        if self.accounts_base_url is not None:
            env_overrides[_ACCOUNTS_URL_SUBPROCESS_ENV] = str(self.accounts_base_url).rstrip("/")
        # Run from $HOME like every other laptop-side mngr invocation, so this
        # does not resolve project config from minds' cwd (the monorepo root in
        # a dev checkout). Otherwise `mngr imbue_cloud auth list` loads
        # `<repo>/.mngr/settings.toml`, which under the e2e test trips mngr's
        # pytest config guard and the account-discovery poll fails every cycle.
        #
        # Debug timing so a slow/timed-out imbue_cloud command tells us which
        # subcommand it was and how long it took before the timeout fired.
        # cg_name uniquely identifies the subcommand; the raw args are
        # deliberately not logged because some callsites (e.g. auth
        # signin/signup) pass secrets like --password.
        logger.debug("Running imbue_cloud command (cg={}, timeout={}s)", cg_name, timeout_seconds)
        start_time = time.monotonic()
        result = self.mngr_caller.call(
            argv,
            timeout=float(timeout_seconds),
            env_overrides=env_overrides,
            cwd=Path.home(),
        )
        logger.debug(
            "Finished imbue_cloud command (cg={}) in {:.1f}s: returncode={} timed_out={}",
            cg_name,
            time.monotonic() - start_time,
            result.returncode,
            result.is_timed_out,
        )
        return result

    def _expect_success(
        self,
        result: MngrCallResult,
        command_repr: str,
        *,
        unavailable_signal: str | None = None,
    ) -> Any:
        if result.returncode == 0:
            return _parse_stdout_json(result.stdout, command_repr)
        exit_code = result.returncode if result.returncode is not None else 1
        if unavailable_signal and unavailable_signal in result.stderr:
            exc = ImbueCloudUnavailableError(f"{command_repr}: connector returned 503 (no matching pool host)")
            exc.exit_code = exit_code
            exc.stdout = result.stdout
            exc.stderr = result.stderr
            raise exc
        if _CLIENT_TOO_OLD_ERROR_CLASS_SIGNAL in result.stderr:
            too_old_message = _parse_stderr_error_message(result.stderr)
            too_old_exc = ImbueCloudClientTooOldCliError(
                too_old_message if too_old_message else CLIENT_TOO_OLD_FALLBACK_MESSAGE
            )
            too_old_exc.exit_code = exit_code
            too_old_exc.stdout = result.stdout
            too_old_exc.stderr = result.stderr
            raise too_old_exc
        if _QUOTA_ERROR_CLASS_SIGNAL in result.stderr:
            quota_message = _parse_stderr_error_message(result.stderr)
            quota_exc = ImbueCloudQuotaExceededCliError(
                f"{command_repr}: {quota_message}" if quota_message else f"{command_repr}: quota exceeded"
            )
            quota_exc.exit_code = exit_code
            quota_exc.stdout = result.stdout
            quota_exc.stderr = result.stderr
            raise quota_exc
        if _LEASE_ACTIVE_CODE_SIGNAL in result.stderr:
            lease_active_exc = ImbueCloudLeaseActiveCliError(
                f"{command_repr}: the workspace still holds its cloud lease; destroy it instead"
            )
            lease_active_exc.exit_code = exit_code
            lease_active_exc.stdout = result.stdout
            lease_active_exc.stderr = result.stderr
            raise lease_active_exc
        if _EMAIL_NOT_VERIFIED_ERROR_CLASS_SIGNAL in result.stderr:
            verification_body = _parse_stderr_error_body(result.stderr) or {}
            verification_message = _parse_stderr_error_message(result.stderr)
            verification_exc = ImbueCloudEmailNotVerifiedCliError(
                f"{command_repr}: {verification_message}"
                if verification_message
                else f"{command_repr}: this action requires a verified email"
            )
            raw_email = verification_body.get("email")
            verification_exc.email = raw_email if isinstance(raw_email, str) and raw_email else None
            verification_exc.exit_code = exit_code
            verification_exc.stdout = result.stdout
            verification_exc.stderr = result.stderr
            raise verification_exc
        auth_failure_body = _parse_auth_failure_body(result.stderr)
        if auth_failure_body is not None:
            auth_message = str(auth_failure_body["error"])
            raw_status = auth_failure_body.get("status")
            auth_exc = ImbueCloudAuthFailedCliError(f"{command_repr}: {auth_message}")
            # A body without a ``status`` is the plugin's own malformed-response
            # guard rather than a connector verdict, so it stays a plain ERROR.
            auth_exc.auth_status = raw_status if isinstance(raw_status, str) and raw_status else "ERROR"
            auth_exc.auth_message = auth_message
            auth_exc.exit_code = exit_code
            auth_exc.stdout = result.stdout
            auth_exc.stderr = result.stderr
            raise auth_exc
        # Log the full subprocess output server-side -- it may be a multi-line
        # Python traceback (e.g. an httpx transport error inside the connector
        # subprocess) -- but keep the exception *message* clean and
        # traceback-free, so routes that surface ``str(exc)`` to an API caller
        # never leak it. The full detail stays on ``.stderr`` for any caller that
        # wants it programmatically.
        logger.warning(
            "{} failed (exit {}); full subprocess output:\n{}",
            command_repr,
            exit_code,
            result.stderr or result.stdout or "(no output)",
        )
        # The plugin reports failures as a JSON body with an ``error`` string --
        # a written sentence ("Session missing in db or has expired"), not a
        # traceback. That is the one thing the user can act on, so carry it.
        # Only when there is no such body (a crash, a non-JSON death) does the
        # message fall back to pointing at the logs, which is all we have.
        error_message = _parse_stderr_error_message(result.stderr)
        plain_exc = ImbueCloudCliError(
            f"{command_repr} failed: {error_message}"
            if error_message
            else f"{command_repr} failed (exit {exit_code}); see the desktop client logs for details"
        )
        plain_exc.exit_code = exit_code
        plain_exc.stdout = result.stdout
        plain_exc.stderr = result.stderr
        raise plain_exc

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def auth_login(
        self,
        success_redirect_url: str | None = None,
        url_file: Path | None = None,
    ) -> ImbueCloudAuthSession:
        """Run the browser login flow (``mngr imbue_cloud auth login``).

        The plugin opens the hosted accounts page in the system browser,
        listens on a localhost loopback for the one-time code, and exchanges
        it (PKCE) for this machine's session. ``url_file`` is where the plugin
        writes the sign-in URL once its listener is live -- the desktop
        client's copy-the-link fallback reads it. Blocks until the flow
        finishes (or the plugin's own 300s timeout).
        """
        args: list[str] = ["auth", "login"]
        if success_redirect_url is not None:
            args.extend(["--success-redirect-url", success_redirect_url])
        if url_file is not None:
            args.extend(["--url-file", str(url_file)])
        result = self._run(args, cg_name="imbue-cloud-auth-login", timeout_seconds=_WEB_LOGIN_TIMEOUT_SECONDS)
        body = self._expect_success(result, "auth login")
        return ImbueCloudAuthSession.model_validate(body)

    def auth_signout(self, account: str) -> None:
        result = self._run(
            ["auth", "signout", "--account", account],
            cg_name="imbue-cloud-auth-signout",
        )
        # Even if the session was already gone, the CLI exits 0 with
        # {"removed": False, "reason": "no session"} -- treat as success.
        self._expect_success(result, "auth signout")

    def auth_status(self, account: str) -> dict[str, Any]:
        result = self._run(
            ["auth", "status", "--account", account],
            cg_name="imbue-cloud-auth-status",
        )
        return self._expect_success(result, "auth status")

    def auth_list(self) -> list[ImbueCloudAuthAccount]:
        """Return the canonical list of signed-in accounts.

        Wraps ``mngr imbue_cloud auth list`` and parses its JSON array
        output into typed records. The plugin owns the SuperTokens
        session store on disk; minds calls this whenever it needs
        identity (UI rendering, bootstrap reconciliation, sharing
        editor) instead of mirroring email/display_name into its own
        files.
        """
        result = self._run(
            ["auth", "list"],
            cg_name="imbue-cloud-auth-list",
        )
        body = self._expect_success(result, "auth list")
        if not isinstance(body, list):
            return []
        return [ImbueCloudAuthAccount.model_validate(entry) for entry in body if isinstance(entry, dict)]

    def auth_refresh(self, account: str) -> dict[str, Any]:
        result = self._run(
            ["auth", "refresh", "--account", account],
            cg_name="imbue-cloud-auth-refresh",
        )
        return self._expect_success(result, "auth refresh")

    def auth_resend_verification(self, account: str) -> bool:
        """Re-send ``account``'s verification email; False when the server cooldown suppressed it."""
        result = self._run(
            ["auth", "resend-verification", "--account", account],
            cg_name="imbue-cloud-auth-resend-verification",
        )
        body = self._expect_success(result, "auth resend-verification")
        sent = body.get("sent") if isinstance(body, dict) else None
        if not isinstance(sent, bool):
            # A missing/non-bool ``sent`` is a broken plugin contract; raising
            # (rather than defaulting to False) keeps the UI from claiming an
            # email "was sent recently" when nothing of the sort is known.
            shape = f"dict with keys {sorted(body)}" if isinstance(body, dict) else type(body).__name__
            raise ImbueCloudCliError(f"Malformed auth resend-verification output: expected a 'sent' bool, got {shape}")
        return sent

    # ------------------------------------------------------------------
    # Hosts (list / release)
    # ------------------------------------------------------------------

    def list_hosts(self, account: str) -> list[LeasedHost]:
        result = self._run(
            ["hosts", "list", "--account", account],
            cg_name="imbue-cloud-hosts-list",
        )
        body = self._expect_success(result, "hosts list")
        if isinstance(body, dict):
            # If the CLI ever emits a wrapped shape, recover the list.
            entries = body.get("hosts", [])
        else:
            entries = body
        if not isinstance(entries, list):
            return []
        return [LeasedHost.model_validate(entry) for entry in entries if isinstance(entry, dict)]

    def release_host(self, account: str, host_db_id: str) -> bool:
        result = self._run(
            ["hosts", "release", host_db_id, "--account", account],
            cg_name="imbue-cloud-hosts-release",
        )
        if result.returncode == 0:
            return True
        logger.warning(
            "imbue_cloud hosts release failed for {} (exit {}): {}",
            host_db_id,
            result.returncode,
            _short(result.stderr or result.stdout),
        )
        return False

    # ------------------------------------------------------------------
    # LiteLLM keys
    # ------------------------------------------------------------------

    def create_litellm_key(
        self,
        *,
        account: str,
        alias: str | None = None,
        max_budget: float | None = None,
        budget_duration: str | None = None,
        metadata: Mapping[str, str] | None = None,
        # Rotate (delete + re-create) an existing key holding ``alias`` inside
        # the single CLI invocation, instead of dead-ending on LiteLLM's
        # unique-alias rejection. Requires ``alias``.
        is_rotate_on_exists: bool = False,
    ) -> LiteLLMKeyMaterial:
        args: list[str] = ["keys", "litellm", "create", "--account", account]
        if alias is not None:
            args.extend(["--alias", alias])
        if max_budget is not None:
            args.extend(["--max-budget", str(max_budget)])
        if budget_duration is not None:
            args.extend(["--budget-duration", budget_duration])
        if metadata is not None:
            args.extend(["--metadata", _json.dumps(dict(metadata))])
        if is_rotate_on_exists:
            args.append("--rotate-on-exists")
        result = self._run(args, cg_name="imbue-cloud-keys-create", timeout_seconds=_KEY_OP_TIMEOUT_SECONDS)
        body = self._expect_success(result, "keys litellm create")
        return LiteLLMKeyMaterial.model_validate(body)

    def list_litellm_keys(self, account: str) -> list[dict[str, Any]]:
        result = self._run(
            ["keys", "litellm", "list", "--account", account],
            cg_name="imbue-cloud-keys-list",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        body = self._expect_success(result, "keys litellm list")
        if isinstance(body, list):
            return body
        return []

    def delete_litellm_key(self, account: str, key_id: str) -> None:
        result = self._run(
            ["keys", "litellm", "delete", key_id, "--account", account],
            cg_name="imbue-cloud-keys-delete",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        self._expect_success(result, "keys litellm delete")

    def update_litellm_key_budget(
        self,
        account: str,
        key_id: str,
        max_budget: float | None,
        budget_duration: str | None = None,
    ) -> None:
        args: list[str] = ["keys", "litellm", "budget", key_id, "--account", account]
        if max_budget is not None:
            args.extend(["--max-budget", str(max_budget)])
        if budget_duration is not None:
            args.extend(["--budget-duration", budget_duration])
        result = self._run(args, cg_name="imbue-cloud-keys-budget", timeout_seconds=_KEY_OP_TIMEOUT_SECONDS)
        self._expect_success(result, "keys litellm budget")

    def get_litellm_key_info(self, account: str, key_id: str) -> dict[str, Any]:
        result = self._run(
            ["keys", "litellm", "show", key_id, "--account", account],
            cg_name="imbue-cloud-keys-show",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        return self._expect_success(result, "keys litellm show")

    # ------------------------------------------------------------------
    # Shares (self-hosted relays)
    # ------------------------------------------------------------------

    def create_share(
        self,
        *,
        account: str,
        host_id: str,
        entry_label: str | None = None,
        preferred_region: str | None = None,
        workspace_id: str | None = None,
    ) -> ShareCliInfo:
        """Enable sharing for a workspace host; the returned relay token is only ever returned here.

        ``entry_label`` is the workspace's shell-service origin label, recorded
        server-side so the hosted web chrome knows the routable origin to enter
        the workspace at; None keeps any previously recorded label.
        ``preferred_region`` steers a first-time share of a local workspace to
        a specific relay region; the connector ignores it for pool hosts and
        keeps an existing share's region.
        """
        args = ["shares", "create", host_id, "--account", account]
        if workspace_id:
            args.extend(["--workspace-id", workspace_id])
        if entry_label:
            args.extend(["--entry-label", entry_label])
        if preferred_region:
            args.extend(["--preferred-region", preferred_region])
        result = self._run(
            args,
            cg_name="imbue-cloud-shares-create",
        )
        body = self._expect_success(result, "shares create")
        if not isinstance(body, dict) or not body.get("workspace_domain"):
            # Describe only the body's shape, never its contents: a well-formed
            # body carries the relay token, which must not leak into an error
            # message that reaches logs and the sharing UI.
            shape = f"dict with keys {sorted(body)}" if isinstance(body, dict) else type(body).__name__
            raise ImbueCloudCliError(f"Malformed shares create output: expected a share object, got {shape}")
        return ShareCliInfo.model_validate({"state": "active", **body})

    def delete_share(self, *, account: str, host_id: str) -> None:
        result = self._run(
            ["shares", "delete", host_id, "--account", account],
            cg_name="imbue-cloud-shares-delete",
        )
        self._expect_success(result, "shares delete")

    def get_share_status(self, *, account: str, host_id: str) -> ShareCliInfo | None:
        """The share's status document, or None when this workspace has never been shared."""
        result = self._run(
            ["shares", "status", host_id, "--account", account],
            cg_name="imbue-cloud-shares-status",
        )
        body = self._expect_success(result, "shares status")
        if not isinstance(body, dict) or body.get("state") in (None, "", "none"):
            return None
        return ShareCliInfo.model_validate(body)

    def list_share_relays(self, *, account: str) -> dict[str, tuple[str, ...]]:
        """The relay fleet as ``{region: tunnel-control endpoints}`` (for latency-based region picking)."""
        result = self._run(
            ["shares", "relays", "--account", account],
            cg_name="imbue-cloud-shares-relays",
        )
        body = self._expect_success(result, "shares relays")
        relays = body.get("relays") if isinstance(body, dict) else None
        if not isinstance(relays, dict):
            raise ImbueCloudCliError("Malformed shares relays output: expected a relays map")
        if not all(isinstance(endpoints, list) for endpoints in relays.values()):
            raise ImbueCloudCliError("Malformed shares relays output: expected an endpoint list per region")
        return {str(region): tuple(str(endpoint) for endpoint in endpoints) for region, endpoints in relays.items()}

    # ------------------------------------------------------------------
    # R2 buckets (one per workspace; used to back up the host_dir via restic)
    # ------------------------------------------------------------------

    def create_bucket(
        self,
        *,
        account: str,
        name: str,
        access: str = "readwrite",
    ) -> R2BucketCreateResult:
        """Create an R2 bucket and mint its default key.

        ``name`` is the short, user-facing bucket name; the connector
        prepends the account's user-id prefix to form the full R2 name
        returned in the result. Raises ``ImbueCloudCliError`` (whose
        ``stderr`` carries the plugin's structured error) on failure --
        the caller distinguishes "already exists" from other failures to
        drive idempotent reuse.
        """
        result = self._run(
            ["bucket", "create", name, "--access", access, "--account", account],
            cg_name="imbue-cloud-bucket-create",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        body = self._expect_success(result, "bucket create")
        return R2BucketCreateResult.model_validate(body)

    def get_bucket_info(self, account: str, name: str) -> R2BucketInfo:
        """Return metadata for the bucket ``name`` (short name) under ``account``."""
        result = self._run(
            ["bucket", "info", name, "--account", account],
            cg_name="imbue-cloud-bucket-info",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        body = self._expect_success(result, "bucket info")
        return R2BucketInfo.model_validate(body)

    def destroy_bucket_force(self, account: str, name: str) -> None:
        """Empty and destroy the bucket ``name`` (short name) under ``account``.

        The plugin CLI empties the bucket client-side (batched S3 deletes,
        taking a cleanup grant when the account's keys are storage-downgraded)
        and then destroys it. The connector refuses to destroy a
        workspace-backup bucket whose workspace record is still ACTIVE, so a
        live workspace's backups can never be deleted through this.
        """
        result = self._run(
            ["bucket", "destroy", name, "--force", "-y", "--account", account],
            cg_name="imbue-cloud-bucket-destroy",
            timeout_seconds=_BUCKET_DESTROY_TIMEOUT_SECONDS,
        )
        self._expect_success(result, "bucket destroy")

    def roll_bucket_key(
        self,
        *,
        account: str,
        name: str,
    ) -> R2BucketKeyMaterial:
        """Roll the bucket's single key (same Access Key ID, fresh secret) and return it.

        Each bucket has exactly one key and the secret is shown only once, so
        this is how re-provisioning gets working credentials for an existing
        bucket.
        """
        result = self._run(
            ["bucket", "roll-key", name, "--account", account],
            cg_name="imbue-cloud-bucket-roll-key",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        body = self._expect_success(result, "bucket roll-key")
        return R2BucketKeyMaterial.model_validate(body)

    # ------------------------------------------------------------------
    # Account (plan + entitlements + usage)
    # ------------------------------------------------------------------

    def get_account_info(self, account: str) -> dict[str, Any]:
        """Return the account's plan, entitlement values, and live usage as a raw dict."""
        result = self._run(
            ["account", "show", "--account", account],
            cg_name="imbue-cloud-account-show",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        return self._expect_success(result, "account show")

    def set_account_plan(self, account: str, plan: str) -> dict[str, Any]:
        """Switch the account's plan; returns ``{plan_name, entitlements}``."""
        result = self._run(
            ["account", "set-plan", plan, "--account", account],
            cg_name="imbue-cloud-account-set-plan",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        return self._expect_success(result, "account set-plan")

    def create_storage_cleanup_grant(self, account: str) -> dict[str, Any]:
        """Temporarily restore storage-downgraded bucket keys so restic cleanup can run.

        Returns the connector's grant body (``status``, ``expires_at``,
        ``baseline_bytes``, ``keys``). Idempotent while a grant is active.
        """
        result = self._run(
            ["account", "cleanup-grant", "--account", account],
            cg_name="imbue-cloud-account-cleanup-grant",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        return self._expect_success(result, "account cleanup-grant")

    def recheck_storage(self, account: str) -> dict[str, Any]:
        """Re-measure live storage usage and apply enforcement immediately.

        Returns the connector's recheck body (``usage_bytes``, ``limit_bytes``,
        ``is_over_quota``, ``is_grant_settled``, ``keys``); settles any
        outstanding cleanup grant.
        """
        result = self._run(
            ["account", "recheck-storage", "--account", account],
            cg_name="imbue-cloud-account-recheck-storage",
            timeout_seconds=_KEY_OP_TIMEOUT_SECONDS,
        )
        return self._expect_success(result, "account recheck-storage")

    # ------------------------------------------------------------------
    # Workspace sync (records + key bundle)
    # ------------------------------------------------------------------

    def sync_records_pull(self, account: str) -> list[dict[str, Any]]:
        result = self._run(["sync", "records", "pull", "--account", account], cg_name="imbue-cloud-sync-records-pull")
        body = self._expect_success(result, "sync records pull")
        records = body.get("records", []) if isinstance(body, dict) else []
        return [entry for entry in records if isinstance(entry, dict)]

    def sync_record_push(self, account: str, record: Mapping[str, Any]) -> dict[str, Any]:
        """Push one record; returns the stored row. Raises ImbueCloudSyncConflictCliError on a 409.

        The record JSON travels via a 0600 temp file (--input-file) so secret
        payloads never ride a command line or a log.
        """
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            os.fchmod(handle.fileno(), 0o600)
            _json.dump(dict(record), handle)
            input_path = handle.name
        try:
            result = self._run(
                ["sync", "records", "push", "--account", account, "--input-file", input_path],
                cg_name="imbue-cloud-sync-record-push",
            )
        finally:
            Path(input_path).unlink(missing_ok=True)
        if result.returncode != 0 and "ImbueCloudSyncConflictError" in result.stderr:
            conflict = ImbueCloudSyncConflictCliError("sync records push: revision/agent conflict")
            conflict.exit_code = result.returncode if result.returncode is not None else 1
            conflict.stdout = result.stdout
            conflict.stderr = result.stderr
            conflict.stored_record = _parse_conflict_stored(result.stderr)
            raise conflict
        body = self._expect_success(result, "sync records push")
        return body if isinstance(body, dict) else {}

    def sync_record_delete(self, account: str, record_id: str) -> None:
        """Delete one record by workspace id (``agent-<hex>``, preferred) or host id.

        Raises ``ImbueCloudLeaseActiveCliError`` when the connector refuses
        because the workspace still holds its pool lease.
        """
        result = self._run(
            ["sync", "records", "delete", record_id, "--account", account],
            cg_name="imbue-cloud-sync-record-delete",
        )
        self._expect_success(result, "sync records delete")

    def sync_scrub_secrets(self, account: str) -> int:
        result = self._run(["sync", "scrub-secrets", "--account", account], cg_name="imbue-cloud-sync-scrub")
        body = self._expect_success(result, "sync scrub-secrets")
        return int(body.get("scrubbed", 0)) if isinstance(body, dict) else 0

    def sync_bundle_pull(self, account: str) -> dict[str, Any] | None:
        result = self._run(["sync", "bundle", "pull", "--account", account], cg_name="imbue-cloud-sync-bundle-pull")
        body = self._expect_success(result, "sync bundle pull")
        bundle = body.get("bundle") if isinstance(body, dict) else None
        return bundle if isinstance(bundle, dict) else None

    def sync_bundle_push(self, account: str, bundle: Mapping[str, Any]) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            os.fchmod(handle.fileno(), 0o600)
            _json.dump(dict(bundle), handle)
            input_path = handle.name
        try:
            result = self._run(
                ["sync", "bundle", "push", "--account", account, "--input-file", input_path],
                cg_name="imbue-cloud-sync-bundle-push",
            )
        finally:
            Path(input_path).unlink(missing_ok=True)
        self._expect_success(result, "sync bundle push")

    def sync_bundle_delete(self, account: str) -> None:
        result = self._run(
            ["sync", "bundle", "delete", "--account", account], cg_name="imbue-cloud-sync-bundle-delete"
        )
        self._expect_success(result, "sync bundle delete")


def _parse_conflict_stored(stderr: str) -> dict[str, Any] | None:
    """Extract the ``stored`` row from a sync-push conflict's JSON error body, if present.

    The body is indent-formatted JSON (its first line is a bare ``{``) that
    may be surrounded by log lines, so each candidate document is raw-decoded
    from the opening brace's actual byte offset -- that consumes exactly one
    document regardless of what precedes or follows it on the stream.
    """
    decoder = _json.JSONDecoder()
    offset = 0
    is_any_document_parsed = False
    for line in stderr.splitlines(keepends=True):
        lstripped = line.lstrip()
        if lstripped.startswith("{"):
            try:
                parsed, _consumed_until = decoder.raw_decode(stderr, offset + len(line) - len(lstripped))
            except _json.JSONDecodeError as exc:
                # Some other output line merely started with a brace; keep
                # scanning for the real error body.
                logger.warning(
                    "Skipping a brace-prefixed non-JSON stderr line while locating the conflict body: {}", exc
                )
                parsed = None
            if isinstance(parsed, dict):
                is_any_document_parsed = True
                stored = parsed.get("stored")
                if isinstance(stored, dict):
                    return stored
        offset += len(line)
    if not is_any_document_parsed:
        logger.warning("Could not locate a JSON error body on the sync-conflict stderr")
    return None


def _parse_stderr_error_body(stderr: str) -> dict[str, Any] | None:
    """Return the plugin's JSON error body from ``stderr``, if one is present.

    Same scanning approach as ``_parse_conflict_stored``: the body is
    indent-formatted JSON that may be surrounded by log lines. Only a document
    carrying a string ``error`` field counts, since that is the shape
    ``fail_with_json`` always emits.
    """
    decoder = _json.JSONDecoder()
    offset = 0
    for line in stderr.splitlines(keepends=True):
        lstripped = line.lstrip()
        if lstripped.startswith("{"):
            try:
                parsed, _consumed_until = decoder.raw_decode(stderr, offset + len(line) - len(lstripped))
            except _json.JSONDecodeError as exc:
                # Some other output line merely started with a brace; keep
                # scanning for the real error body.
                logger.warning("Skipping a brace-prefixed non-JSON stderr line while locating the error body: {}", exc)
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                return parsed
        offset += len(line)
    return None


def _parse_stderr_error_message(stderr: str) -> str | None:
    """Extract the ``error`` message from the plugin's JSON stderr body, if present."""
    body = _parse_stderr_error_body(stderr)
    return None if body is None else str(body["error"])


def _parse_auth_failure_body(stderr: str) -> dict[str, Any] | None:
    """Return the plugin's structured auth-rejection body, or None if this isn't one."""
    body = _parse_stderr_error_body(stderr)
    if body is None or body.get("error_class") != _AUTH_FAILED_ERROR_CLASS:
        return None
    return body


def _parse_stdout_json(stdout: str, command_repr: str) -> Any:
    """Parse the JSON document the plugin emits on a successful invocation.

    The plugin always writes a single trailing-newline-terminated JSON document
    (object or list) on stdout for success.
    """
    text = stdout.strip()
    if not text:
        empty_exc = ImbueCloudCliError(f"{command_repr}: empty stdout from plugin")
        empty_exc.exit_code = 0
        empty_exc.stdout = stdout
        raise empty_exc
    try:
        return _json.loads(text)
    except _json.JSONDecodeError as exc:
        logger.error("Failed to parse JSON from {}: {}", command_repr, exc)
        bad_json_exc = ImbueCloudCliError(f"{command_repr}: stdout was not JSON: {_short(text)}")
        bad_json_exc.exit_code = 0
        bad_json_exc.stdout = stdout
        raise bad_json_exc from exc


def _short(text: str, limit: int = 400) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"
