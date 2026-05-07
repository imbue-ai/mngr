"""Typed wrapper around the ``mngr imbue_cloud …`` CLI surface.

Every operation that minds previously did via direct HTTP calls into the
``remote_service_connector`` (auth, host pool, LiteLLM keys, Cloudflare
tunnels) now runs as a child process invocation of ``mngr imbue_cloud …``,
spawned through a ``ConcurrencyGroup`` so failures and lifetimes are managed
the same way as every other subprocess minds drives.

The plugin always emits a JSON document on stdout for the success case and a
JSON ``{"error": ...}`` document on stderr for the failure case (see
``libs/mngr_imbue_cloud/imbue/mngr_imbue_cloud/cli/_common.py``); this module
parses those into typed pydantic objects.
"""

import json as _json
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from loguru import logger
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds.errors import MindError

_MNGR_BINARY = "mngr"
_DEFAULT_TIMEOUT_SECONDS = 60.0
_LEASE_TIMEOUT_SECONDS = 300.0
_KEY_OP_TIMEOUT_SECONDS = 90.0

# Mirrors ``apps/remote_service_connector/.../app.py`` -- the connector uses
# the first 16 hex chars of the agent UUID (after stripping ``"agent-"``) as
# the trailing slug of every tunnel name. Used by ``find_tunnel_for_agent``
# to filter ``list_tunnels`` output without having to know the per-account
# username prefix.
_AGENT_ID_PREFIX_LENGTH = 16


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


class ImbueCloudAuthSession(FrozenModel):
    """Result of a successful auth signin/signup/oauth invocation."""

    user_id: str
    email: str
    display_name: str | None = None
    needs_email_verification: bool = False


class ImbueCloudAuthAccount(FrozenModel):
    """One entry from `mngr imbue_cloud auth list`."""

    user_id: str
    email: str
    display_name: str | None = None
    is_active: bool = False


class LeasedHost(FrozenModel):
    """One row of `mngr imbue_cloud hosts list`."""

    host_db_id: str
    host_id: str
    agent_id: str
    vps_ip: str
    ssh_user: str
    ssh_port: int
    container_ssh_port: int
    attributes: dict[str, Any] = Field(default_factory=dict)
    leased_at: str


class LiteLLMKeyMaterial(FrozenModel):
    """Result of `mngr imbue_cloud keys litellm create`."""

    key: SecretStr
    base_url: AnyUrl


class TunnelInfo(FrozenModel):
    """Result of `mngr imbue_cloud tunnels create` / list entry."""

    tunnel_name: str
    tunnel_id: str
    token: SecretStr | None = None
    services: tuple[str, ...] = ()


class ImbueCloudCli(MutableModel):
    """Run ``mngr imbue_cloud …`` subcommands inside a ConcurrencyGroup.

    All invocations are routed through ``ConcurrencyGroup.run_process_to_completion``
    so the calling code's resource lifetime extends to cover the subprocess.
    """

    parent_concurrency_group: ConcurrencyGroup = Field(
        frozen=True,
        description=(
            "Parent CG. Each invocation creates a child group named after the subcommand "
            "so subprocesses are tied to the desktop client's overall lifetime."
        ),
    )

    def _run(
        self,
        args: Sequence[str],
        *,
        cg_name: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        on_output: Any = None,
    ) -> FinishedProcess:
        full_command = [_MNGR_BINARY, "imbue_cloud", *args]
        cg = self.parent_concurrency_group.make_concurrency_group(name=cg_name)
        with cg:
            return cg.run_process_to_completion(
                command=full_command,
                timeout=float(timeout_seconds),
                is_checked_after=False,
                on_output=on_output,
            )

    def _expect_success(
        self,
        result: FinishedProcess,
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
        plain_exc = ImbueCloudCliError(
            f"{command_repr} failed (exit {exit_code}): {_short(result.stderr or result.stdout)}"
        )
        plain_exc.exit_code = exit_code
        plain_exc.stdout = result.stdout
        plain_exc.stderr = result.stderr
        raise plain_exc

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def auth_signin(self, account: str, password: str) -> ImbueCloudAuthSession:
        result = self._run(
            ["auth", "signin", "--account", account, "--password", password],
            cg_name="imbue-cloud-auth-signin",
        )
        body = self._expect_success(result, "auth signin")
        return ImbueCloudAuthSession.model_validate(body)

    def auth_signup(self, account: str, password: str) -> ImbueCloudAuthSession:
        result = self._run(
            ["auth", "signup", "--account", account, "--password", password],
            cg_name="imbue-cloud-auth-signup",
        )
        body = self._expect_success(result, "auth signup")
        return ImbueCloudAuthSession.model_validate(body)

    def auth_oauth(
        self,
        account: str,
        provider_id: str,
        callback_port: int | None = None,
        no_browser: bool = False,
    ) -> ImbueCloudAuthSession:
        args: list[str] = [
            "auth",
            "oauth",
            provider_id,
            "--account",
            account,
        ]
        if callback_port is not None:
            args.extend(["--callback-port", str(callback_port)])
        if no_browser:
            args.append("--no-browser")
        result = self._run(args, cg_name="imbue-cloud-auth-oauth", timeout_seconds=_LEASE_TIMEOUT_SECONDS)
        body = self._expect_success(result, "auth oauth")
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
    # Tunnels
    # ------------------------------------------------------------------

    def create_tunnel(
        self,
        *,
        account: str,
        agent_id: str,
        default_policy: Mapping[str, Any] | None = None,
    ) -> TunnelInfo:
        args: list[str] = ["tunnels", "create", agent_id, "--account", account]
        if default_policy is not None:
            args.extend(["--policy", _json.dumps(dict(default_policy))])
        result = self._run(args, cg_name="imbue-cloud-tunnels-create")
        body = self._expect_success(result, "tunnels create")
        return TunnelInfo.model_validate(body)

    def list_tunnels(self, account: str) -> list[TunnelInfo]:
        result = self._run(
            ["tunnels", "list", "--account", account],
            cg_name="imbue-cloud-tunnels-list",
        )
        body = self._expect_success(result, "tunnels list")
        if isinstance(body, list):
            return [TunnelInfo.model_validate(entry) for entry in body if isinstance(entry, dict)]
        return []

    def delete_tunnel(self, account: str, tunnel_name: str) -> None:
        result = self._run(
            ["tunnels", "delete", tunnel_name, "--account", account],
            cg_name="imbue-cloud-tunnels-delete",
        )
        self._expect_success(result, "tunnels delete")

    def add_service(
        self,
        *,
        account: str,
        tunnel_name: str,
        service_name: str,
        service_url: str,
    ) -> dict[str, Any]:
        result = self._run(
            ["tunnels", "services", "add", tunnel_name, service_name, service_url, "--account", account],
            cg_name="imbue-cloud-services-add",
        )
        return self._expect_success(result, "tunnels services add")

    def list_services(self, account: str, tunnel_name: str) -> list[dict[str, Any]]:
        result = self._run(
            ["tunnels", "services", "list", tunnel_name, "--account", account],
            cg_name="imbue-cloud-services-list",
        )
        body = self._expect_success(result, "tunnels services list")
        if isinstance(body, list):
            return body
        return []

    def remove_service(self, account: str, tunnel_name: str, service_name: str) -> None:
        result = self._run(
            ["tunnels", "services", "remove", tunnel_name, service_name, "--account", account],
            cg_name="imbue-cloud-services-remove",
        )
        self._expect_success(result, "tunnels services remove")

    def set_tunnel_auth(self, account: str, tunnel_name: str, policy: Mapping[str, Any]) -> None:
        result = self._run(
            ["tunnels", "auth", "set", tunnel_name, _json.dumps(dict(policy)), "--account", account],
            cg_name="imbue-cloud-tunnel-auth-set",
        )
        self._expect_success(result, "tunnels auth set")

    def get_tunnel_auth(self, account: str, tunnel_name: str) -> dict[str, Any]:
        result = self._run(
            ["tunnels", "auth", "get", tunnel_name, "--account", account],
            cg_name="imbue-cloud-tunnel-auth-get",
        )
        return self._expect_success(result, "tunnels auth get")

    def set_service_auth(
        self,
        account: str,
        tunnel_name: str,
        service_name: str,
        policy: Mapping[str, Any],
    ) -> None:
        """Set the per-service auth policy on a tunnel.

        Wraps ``mngr imbue_cloud tunnels auth set <tunnel_name> <policy_json> --service <name>``.
        Pass ``policy`` as ``{"emails": [...], "email_domains": [...], "require_idp": ...}``;
        empty fields are accepted as defaults by the plugin.
        """
        result = self._run(
            [
                "tunnels",
                "auth",
                "set",
                tunnel_name,
                _json.dumps(dict(policy)),
                "--service",
                service_name,
                "--account",
                account,
            ],
            cg_name="imbue-cloud-service-auth-set",
        )
        self._expect_success(result, "tunnels auth set --service")

    def get_service_auth(self, account: str, tunnel_name: str, service_name: str) -> dict[str, Any]:
        """Read the per-service auth policy from a tunnel.

        Wraps ``mngr imbue_cloud tunnels auth get <tunnel_name> --service <name>``.
        Returns the same ``AuthPolicy`` JSON shape as :meth:`get_tunnel_auth`.
        """
        result = self._run(
            ["tunnels", "auth", "get", tunnel_name, "--service", service_name, "--account", account],
            cg_name="imbue-cloud-service-auth-get",
        )
        return self._expect_success(result, "tunnels auth get --service")

    def find_tunnel_for_agent(self, account: str, agent_id: str) -> TunnelInfo | None:
        """Return the tunnel registered for ``agent_id`` under ``account``, or None.

        Uses ``list_tunnels`` and matches on the trailing-slug convention the
        connector uses for tunnel names: ``<short_user>--<short_agent>``,
        where ``short_agent`` is the first 16 hex chars of the agent UUID
        (``"agent-"`` prefix stripped). Stable contract -- changing the
        truncation length on the connector side requires updating
        ``_AGENT_ID_PREFIX_LENGTH`` here in lockstep.

        Returning ``None`` lets the sharing-status route distinguish
        "tunnel doesn't exist yet" (the user hasn't enabled sharing) from
        "tunnel exists but no service is registered for this name".
        """
        short_agent = agent_id.removeprefix("agent-")[:_AGENT_ID_PREFIX_LENGTH]
        suffix = f"--{short_agent}"
        for tunnel in self.list_tunnels(account):
            if tunnel.tunnel_name.endswith(suffix):
                return tunnel
        return None


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
