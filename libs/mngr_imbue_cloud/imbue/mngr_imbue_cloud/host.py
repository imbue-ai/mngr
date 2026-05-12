"""Host class for imbue_cloud-leased agents.

Subclasses mngr's ``Host`` so the standard ``mngr create --provider
imbue_cloud_<account> --new-host`` pipeline can adopt a pool host's
pre-baked agent under the caller's chosen name when one exists, and
fall back to mngr's standard create flow when it doesn't (e.g. after
``mngr destroy`` has wiped the previous agent's state on the leased
container). Adoption is purely an optimization that skips a slow
file-transfer + provisioning round when we can.

Overrides:

- ``set_env_vars`` always merges into the pre-baked ``/mngr/env``
  (clobbering would lose ``MNGR_HOST_DIR``/``MNGR_PREFIX``/etc. that
  the pool baking wrote).
- ``create_agent_state`` always pins ``options.agent_id`` to
  ``pre_baked_agent_id`` so the lease's canonical id stays stable
  across destroy/recreate cycles, regardless of whether on-disk state
  survives. The parent's ``data.json`` write then runs as usual.
- ``create_agent_work_dir`` and ``provision_agent`` short-circuit to a
  no-transfer + minimal-provision path *only* when the pre-baked
  agent's ``data.json`` is still on disk; otherwise they delegate to
  ``super()`` and let mngr do a full create + provision.
"""

import json as _json
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from pydantic import Field

from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import CreateWorkDirResult
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName


def _parse_create_time(value: Any) -> datetime:
    """Parse a ``create_time`` ISO string from a serialized ``data.json``.

    Returns the parsed datetime. Falls back to ``datetime.now()`` when the
    field is missing or malformed; both cases are unexpected on a healthy
    pool host but still leave us with a usable agent rather than crashing
    create_agent_state.
    """
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class ImbueCloudHost(Host):
    """A leased pool host.

    The pre-baked agent's id is captured at lease time so ``create_agent_state``
    can adopt that agent under the caller's name instead of generating a new id.
    """

    pre_baked_agent_id: AgentId | None = Field(
        default=None,
        frozen=True,
        description=(
            "Agent id of the agent that was pre-provisioned on this pool host. "
            "Set by the provider when the host is created via lease."
        ),
    )
    lease_db_id: str | None = Field(
        default=None,
        frozen=True,
        description="Database id of this lease (UUID returned by /hosts/lease).",
    )

    def set_env_vars(self, env: Mapping[str, str]) -> None:
        """Merge ``env`` into the pre-baked ``/mngr/env`` instead of overwriting.

        The pool host's host env file already contains values that the agent
        runtime needs (``MNGR_HOST_DIR``, ``MNGR_PREFIX``, etc.). The standard
        ``Host.set_env_vars`` would clobber them, so we read-modify-write to
        keep the pre-baked entries that the caller didn't override.
        """
        if not env:
            return
        existing = self.get_env_vars()
        existing.update(env)
        super().set_env_vars(existing)

    def _read_pre_baked_data(self) -> dict[str, Any] | None:
        """Try to read the pre-baked agent's ``data.json`` from the leased container.

        Returns the parsed dict when present, ``None`` when this host has
        no ``pre_baked_agent_id`` (constructed outside the lease flow) or
        the file is missing on disk (e.g. ``mngr destroy`` deleted the
        agent state on a previous lease cycle). Callers use this to
        decide whether the optimized adopt path is available; ``None``
        means "fall back to mngr's standard create flow".
        """
        if self.pre_baked_agent_id is None:
            return None
        data_path = self.host_dir / "agents" / str(self.pre_baked_agent_id) / "data.json"
        try:
            return _json.loads(self.read_text_file(data_path))
        except FileNotFoundError:
            return None

    def create_agent_work_dir(
        self,
        host: OnlineHostInterface,
        path: Path,
        options: CreateAgentOptions,
    ) -> CreateWorkDirResult:
        """Adopt the pre-baked work_dir when one is on disk, otherwise transfer normally.

        When the pre-baked agent's ``data.json`` is still present on the
        leased container (the common case, right after a fresh lease), we
        skip the file transfer and return the recorded ``work_dir`` -- the
        FCT template baked it (``target_path = "/code/"`` for the vultr
        template, etc.) and we just trust whatever was written.

        Otherwise, fall through to mngr's standard ``create_agent_work_dir``
        which runs the configured transfer mode against ``host`` / ``path``.
        """
        data = self._read_pre_baked_data()
        if data is not None:
            recorded_work_dir = data.get("work_dir")
            if isinstance(recorded_work_dir, str) and recorded_work_dir:
                return CreateWorkDirResult(path=Path(recorded_work_dir))
        return super().create_agent_work_dir(host, path, options)

    def create_agent_state(
        self,
        work_dir_path: Path,
        options: CreateAgentOptions,
        created_branch_name: str | None = None,
    ) -> AgentInterface:
        """Adopt the pre-baked agent's state instead of writing a fresh ``data.json``.

        When ``pre_baked_agent_id`` is set (i.e. this is the leased-pool-host
        path), we *load* the existing ``data.json`` from the host -- which the
        bake wrote with ``--template main --template vultr`` and therefore
        already contains the ``additional_commands`` that start
        ``minds-workspace-server``, ``cloudflared``, etc. -- and patch only
        the minds-driven fields in place: ``name``, ``labels.workspace``,
        and ``command`` (regenerated via ``assemble_command`` so the embedded
        ``<MNGR_PREFIX><name>`` tmux session reference matches the new name).
        Everything else (``additional_commands``, ``create_time``, ``work_dir``,
        ``agent_type``, the agent UUID baked into the ``--session-id``
        fallback) is preserved verbatim from the bake.

        The previous version of this method just pinned ``options.agent_id``
        and called ``super().create_agent_state``, which unconditionally
        wrote a brand-new ``data.json`` from ``CreateAgentOptions``. For the
        adopt path that clobbered the bake's ``additional_commands`` (so
        the workspace server never started on lease) and was incompatible
        with minds' newer "don't pre-generate an agent id" model anyway
        (``options.agent_id`` is now always ``None`` from the minds side).

        Falls back to ``super()`` when ``pre_baked_agent_id`` is unset or
        the on-disk ``data.json`` is missing -- e.g. after ``mngr destroy``
        wiped the previous lease cycle's state and we need a full create.
        """
        if self.pre_baked_agent_id is None:
            return super().create_agent_state(work_dir_path, options, created_branch_name)
        if options.agent_id is not None and options.agent_id != self.pre_baked_agent_id:
            raise ValueError(
                f"imbue_cloud agent id is fixed by the lease ({self.pre_baked_agent_id}); "
                f"caller requested {options.agent_id}. Drop --id to let the lease decide."
            )

        existing = self._read_pre_baked_data()
        if existing is None:
            # Lease said pre-baked, but the file is gone -- previous cycle's
            # ``mngr destroy`` cleaned the agent state. Fall through to the
            # standard create path so mngr writes a fresh ``data.json`` (this
            # path will lose the bake's ``additional_commands``; if that
            # matters here we want a louder failure mode, but that's a
            # different conversation than the lease-adopt happy path).
            options_with_id = options.model_copy(update={"agent_id": self.pre_baked_agent_id})
            return super().create_agent_state(work_dir_path, options_with_id, created_branch_name)

        # Hydrate the agent class so we can re-run ``assemble_command`` against
        # the user-supplied name + the bake's UUID-derived session-id fallback.
        agent_type = AgentTypeName(str(existing.get("type", "claude")))
        resolved = resolve_agent_type(agent_type, self.mngr_ctx.config)
        baked_work_dir = Path(str(existing.get("work_dir", str(work_dir_path))))
        create_time = _parse_create_time(existing.get("create_time"))
        new_name = options.name or AgentName(str(existing["name"]))

        agent = resolved.agent_class(
            id=self.pre_baked_agent_id,
            name=new_name,
            agent_type=agent_type,
            work_dir=baked_work_dir,
            create_time=create_time,
            host_id=self.id,
            host=self,
            mngr_ctx=self.mngr_ctx,
            agent_config=resolved.agent_config,
        )
        new_command = agent.assemble_command(
            host=self,
            agent_args=options.agent_args,
            command_override=options.command,
            initial_message=options.initial_message,
        )

        # Merge labels: bake's defaults + minds' user-supplied (latter wins).
        # Update ``workspace`` to track the new name so the minds UI's "agents
        # for workspace foo" lookup finds this agent.
        merged_labels: dict[str, str] = dict(existing.get("labels") or {})
        merged_labels.update(options.label_options.labels)
        merged_labels["workspace"] = str(new_name)

        # Build the new data dict by patching the existing one in place. Any
        # bake-time fields we don't explicitly touch (``additional_commands``,
        # ``permissions``, ``start_on_boot``, ``initial_message`` /
        # ``resume_message`` / ``ready_timeout_seconds`` if minds didn't
        # supply replacements, etc.) survive untouched. ``agent_id`` stays
        # the bake's pre-baked id by construction.
        patched: dict[str, Any] = dict(existing)
        patched["name"] = str(new_name)
        patched["command"] = str(new_command)
        patched["labels"] = merged_labels
        if options.initial_message is not None:
            patched["initial_message"] = options.initial_message
        if options.resume_message is not None:
            patched["resume_message"] = options.resume_message
        if options.ready_timeout_seconds is not None:
            patched["ready_timeout_seconds"] = options.ready_timeout_seconds
        if created_branch_name is not None:
            patched["created_branch_name"] = created_branch_name

        data_path = self.host_dir / "agents" / str(self.pre_baked_agent_id) / "data.json"
        self.write_text_file(data_path, _json.dumps(patched, indent=2))
        return agent

    def provision_agent(
        self,
        agent: AgentInterface,
        options: CreateAgentOptions,
        mngr_ctx: MngrContext,
    ) -> None:
        """Minimal provisioning when the pool host is already provisioned, full otherwise.

        When the pre-baked agent's ``data.json`` is still on disk, the
        container has all the packages and file transfers the FCT template
        installed and we only need to (a) write the agent env file (so
        ``MNGR_AGENT_NAME`` / ``--env`` overrides land) and (b) patch the
        claude config when ``ANTHROPIC_API_KEY`` is set anywhere in env
        (the LiteLLM key flows through ``--pass-host-env`` for minds, so
        we have to look at host env, not just agent env).

        When the pre-baked agent state has been wiped (``mngr destroy``
        on a previous lease cycle, etc.), fall through to mngr's standard
        ``provision_agent`` so packages/file transfers/agent-type provisioning
        run from scratch.
        """
        if self._read_pre_baked_data() is None:
            super().provision_agent(agent, options, mngr_ctx)
            return
        agent_env = self._collect_agent_env_vars(agent, options)
        self._write_agent_env_file(agent, agent_env)
        anthropic_api_key = agent_env.get("ANTHROPIC_API_KEY") or self.get_env_vars().get("ANTHROPIC_API_KEY")
        if anthropic_api_key:
            patch_command = _build_patch_claude_config_command(anthropic_api_key, agent.id)
            result = self.execute_idempotent_command(patch_command)
            if not result.success:
                raise RuntimeError(f"Failed to patch claude config on imbue_cloud host {self.id}: {result.stderr}")


def build_combined_inject_command(
    agent_id: AgentId,
    agent_env_path: str,
    host_env_path: str,
    minds_api_key: str | None,
    anthropic_api_key: str | None,
    anthropic_base_url: str | None,
    mngr_prefix: str | None,
    extra_env: Mapping[str, str] | None = None,
) -> str | None:
    """Build the single bash command that injects credentials into a leased agent.

    Mirrors the consolidated approach in ``apps/minds/imbue/minds/desktop_client/
    agent_creator.py`` (commit b65f52ac4): all writes are joined with ``&&`` so a
    single SSH round trip is sufficient and partial-failure leaves no
    half-injected state.

    Returns None when there is nothing to inject (caller should skip the exec).
    """
    pieces: list[str] = []

    if minds_api_key is not None:
        pieces.append(_sed_replace_env_line(agent_env_path, "MINDS_API_KEY", minds_api_key))

    if anthropic_api_key is not None:
        pieces.append(_sed_replace_env_line(host_env_path, "ANTHROPIC_API_KEY", anthropic_api_key))

    if anthropic_base_url is not None:
        pieces.append(_sed_replace_env_line(host_env_path, "ANTHROPIC_BASE_URL", anthropic_base_url))

    if anthropic_api_key is not None:
        pieces.append(_build_patch_claude_config_command(anthropic_api_key, agent_id))

    if mngr_prefix is not None:
        pieces.append(_sed_replace_env_line(host_env_path, "MNGR_PREFIX", mngr_prefix))

    if extra_env:
        for key, value in extra_env.items():
            pieces.append(_sed_replace_env_line(host_env_path, key, value))

    if not pieces:
        return None
    return " && ".join(pieces)


def _sed_replace_env_line(path: str, var_name: str, value: str) -> str:
    """Build a sed+echo expression that replaces ``KEY=...`` in an env file.

    Uses sed -i to delete any prior occurrence of the var, then appends a fresh
    KEY=VALUE line. The value is single-quoted in the echo, but for safety we
    escape embedded single quotes using the standard ``'\\''`` trick.
    """
    safe_value = value.replace("'", "'\\''")
    return f"sed -i '/^{var_name}=/d' {path} && echo '{var_name}={safe_value}' >> {path}"


def _build_patch_claude_config_command(litellm_key: str, agent_id: AgentId) -> str:
    """Build a python one-liner that patches the agent's claude config to approve the new key.

    Mirrors ``_build_patch_claude_config_command`` in minds' agent_creator.py.
    """
    claude_config_path = f"/mngr/agents/{agent_id}/plugin/claude/anthropic/.claude.json"
    key_suffix = litellm_key[-20:]
    return (
        'python3 -c "'
        "import json; "
        f"p='{claude_config_path}'; "
        "d=json.load(open(p)); "
        f"d['primaryApiKey']='{litellm_key}'; "
        "a=d.setdefault('customApiKeyResponses',{}).setdefault('approved',[]); "
        f"s='{key_suffix}'; "
        "a.append(s) if s not in a else None; "
        "d['customApiKeyResponses']['rejected']=[]; "
        "json.dump(d,open(p,'w'),indent=2)"
        '"'
    )


def _ensure_no_quote_chars(value: str, field_name: str) -> str:
    """Defensive guard so injected values can't break out of the sed/echo quoting.

    Raises ValueError if the value contains characters that we don't escape.
    """
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise ValueError(f"{field_name} cannot contain newlines or null bytes: {value!r}")
    return value


def normalize_inject_args(
    minds_api_key: str | None,
    anthropic_api_key: str | None,
    anthropic_base_url: str | None,
    mngr_prefix: str | None,
    extra_env: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Validate and clean inject arguments before they are interpolated into bash.

    Centralized here so the claim CLI and any future caller share the same checks.
    """
    cleaned_extra: dict[str, str] = {}
    if extra_env:
        for key, value in extra_env.items():
            if not key or "=" in key:
                raise ValueError(f"Invalid env var name: {key!r}")
            cleaned_extra[key] = _ensure_no_quote_chars(value, f"env[{key}]")
    return {
        "minds_api_key": _ensure_no_quote_chars(minds_api_key, "MINDS_API_KEY") if minds_api_key else None,
        "anthropic_api_key": _ensure_no_quote_chars(anthropic_api_key, "ANTHROPIC_API_KEY")
        if anthropic_api_key
        else None,
        "anthropic_base_url": _ensure_no_quote_chars(anthropic_base_url, "ANTHROPIC_BASE_URL")
        if anthropic_base_url
        else None,
        "mngr_prefix": _ensure_no_quote_chars(mngr_prefix, "MNGR_PREFIX") if mngr_prefix else None,
        "extra_env": cleaned_extra or None,
    }


def host_label_for_agent(agent_name: AgentName) -> str:
    """Default host name suffix for an agent (matches today's minds convention)."""
    return f"{agent_name}-host"
