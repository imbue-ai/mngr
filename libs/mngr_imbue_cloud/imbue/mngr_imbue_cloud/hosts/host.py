"""Host class for imbue_cloud-leased agents.

Subclasses mngr's ``Host`` so the standard ``mngr create --provider
imbue_cloud_<account> --new-host`` pipeline can adopt a pool host's
pre-baked agent when one exists, and fall back to mngr's standard
create flow when it doesn't (e.g. after ``mngr destroy`` has wiped the
previous agent's state on the leased container). Adoption is purely an
optimization that skips a slow file-transfer + provisioning round when
we can. The workspace identity lives on the *host name*; the adopted
agent keeps the bake's name (a constant such as ``system-services``)
verbatim.

Overrides:

- ``set_env_vars`` always merges into the pre-baked ``/mngr/env``
  (clobbering would lose ``MNGR_HOST_DIR``/``MNGR_PREFIX``/etc. that
  the pool baking wrote).
- ``create_agent_state`` preserves the bake's name and patches the
  on-disk ``data.json`` in place when the pre-baked agent state is
  present. It rejects an ``options.agent_id`` that mismatches
  ``pre_baked_agent_id`` (the lease dictates the id) and, in the
  fallback path where ``data.json`` is missing, pins ``options.agent_id``
  to the pre-baked id before delegating to ``super()``.
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
from imbue.mngr.hosts.common import get_agent_state_dir_path
from imbue.mngr.hosts.host import Host
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import CreateAgentOptions
from imbue.mngr.interfaces.host import CreateWorkDirResult
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr_imbue_cloud.errors import ClaudeConfigPatchError
from imbue.mngr_imbue_cloud.errors import FixedAgentIdError


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

    The pre-baked agent's id is captured at lease time so
    ``create_agent_state`` can adopt that agent (keeping the bake's name
    and id) instead of generating a fresh ``data.json``. The workspace
    identity is carried by the host name; the agent name is whatever the
    bake wrote (typically a constant such as ``system-services``).
    """

    # ``pre_baked_agent_id`` is inherited from the base ``Host`` class
    # (default None on every other provider's hosts; this provider's
    # ``create_host`` populates it from the lease). Keeping it on the base
    # lets ``api/create.py``'s duplicate-name check recognize the adopt
    # scenario without a getattr-on-host shim.
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
        data_path = get_agent_state_dir_path(self.host_dir, self.pre_baked_agent_id) / "data.json"
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
        ``system-interface``, ``cloudflared``, etc. -- and patch only
        the minds-driven fields in place: ``labels`` and ``command``
        (regenerated via ``assemble_command`` so the embedded
        ``<MNGR_PREFIX><name>`` tmux session reference still resolves).
        Everything else (``name``, ``additional_commands``, ``create_time``,
        ``work_dir``, ``agent_type``, the agent UUID baked into the
        ``--session-id`` fallback) is preserved verbatim from the bake --
        the workspace identity now lives on the *host*, not on the agent.

        Falls back to ``super()`` when ``pre_baked_agent_id`` is unset or
        the on-disk ``data.json`` is missing -- e.g. after ``mngr destroy``
        wiped the previous lease cycle's state and we need a full create.
        """
        if self.pre_baked_agent_id is None:
            return super().create_agent_state(work_dir_path, options, created_branch_name)
        if options.agent_id is not None and options.agent_id != self.pre_baked_agent_id:
            raise FixedAgentIdError(
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

        # Hydrate the agent class with the bake's name; minds no longer
        # renames the pre-baked agent (the workspace identity lives on the
        # host now). ``options.name`` is the minds-supplied default agent
        # name ("system-services"), kept around for non-imbue_cloud modes;
        # for adoption we ignore it.
        agent_type = AgentTypeName(str(existing.get("type", "claude")))
        resolved = resolve_agent_type(agent_type, self.mngr_ctx.config)
        baked_work_dir = Path(str(existing.get("work_dir", str(work_dir_path))))
        create_time = _parse_create_time(existing.get("create_time"))
        baked_name = AgentName(str(existing["name"]))

        agent = resolved.agent_class(
            id=self.pre_baked_agent_id,
            name=baked_name,
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
        # Minds passes ``--label workspace=<host_name>`` so the workspace
        # identity is propagated via the caller's labels; we don't re-derive
        # it from any agent name.
        merged_labels: dict[str, str] = dict(existing.get("labels") or {})
        merged_labels.update(options.label_options.labels)

        # Build the new data dict by patching the existing one in place. Any
        # bake-time fields we don't explicitly touch (``additional_commands``,
        # ``permissions``, ``start_on_boot``, ``initial_message`` /
        # ``resume_message`` / ``ready_timeout_seconds`` if minds didn't
        # supply replacements, etc.) survive untouched. ``agent_id`` and
        # ``name`` stay the bake's by construction.
        patched: dict[str, Any] = dict(existing)
        patched["command"] = str(new_command)
        patched["labels"] = merged_labels
        if options.initial_message is not None:
            patched["initial_message"] = options.initial_message
        if options.resume_message is not None:
            patched["resume_message"] = options.resume_message
        if options.ready_timeout_seconds is not None:
            patched["ready_timeout_seconds"] = options.ready_timeout_seconds
        # Retarget the bake's pre-set branch to the minds-supplied per-host
        # branch when the caller passes one. Caller-supplied wins; otherwise
        # leave the bake's value intact (e.g. when an external mngr CLI user
        # invokes the lease flow without driving branch naming).
        if created_branch_name is not None:
            patched["created_branch_name"] = created_branch_name

        data_path = get_agent_state_dir_path(self.host_dir, self.pre_baked_agent_id) / "data.json"
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
                raise ClaudeConfigPatchError(
                    f"Failed to patch claude config on imbue_cloud host {self.id}: {result.stderr}"
                )


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
