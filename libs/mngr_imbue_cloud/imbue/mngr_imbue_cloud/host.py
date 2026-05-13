"""Host class for imbue_cloud-leased agents.

Subclasses mngr's ``Host`` so the standard ``mngr create --provider
imbue_cloud_<account> --new-host`` pipeline can adopt a pool host's
pre-baked agent verbatim when one exists, and fall back to mngr's
standard create flow when it doesn't (e.g. after ``mngr destroy`` has
wiped the previous agent's state on the leased container). Adoption is
purely an optimization that skips a slow file-transfer + provisioning
round when we can.

The user-facing workspace name lives on the *host* (set on the lease
record via the connector's ``host_name`` field, surfaced through
``HostName``), not on the agent. The pre-baked agent's name and command
are kept verbatim from the bake (today's bake produces a system-services
agent that is never renamed). Labels are merged: bake-supplied defaults
(e.g. ``is_primary=true``) plus the caller's ``--label`` flags (e.g.
``workspace=<workspace_name>``), with the caller winning on key
collisions.

Overrides:

- ``set_env_vars`` always merges into the pre-baked ``/mngr/env``
  (clobbering would lose ``MNGR_HOST_DIR``/``MNGR_PREFIX``/etc. that
  the pool baking wrote).
- ``create_agent_state`` adopts the pre-baked agent's ``data.json``
  but merges caller-supplied labels (and persists ``created_branch_name``
  when supplied) when it exists; otherwise pins ``options.agent_id``
  to ``pre_baked_agent_id`` and delegates to ``super()`` so the
  lease's canonical id stays stable across destroy/recreate cycles.
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

    The pre-baked agent's id is captured at lease time so
    ``create_agent_state`` can adopt the bake's agent verbatim (its name
    ``system-services`` and command are kept; caller-supplied ``--label``
    flags are merged on top of the bake's defaults) and so the canonical
    agent id stays the bake's id rather than being regenerated. The
    user-facing workspace name lives on the *host* (via the lease's
    ``host_name`` field), not on the agent.
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
        """Adopt the pre-baked agent's state, merging in caller-supplied labels.

        When ``pre_baked_agent_id`` is set (i.e. this is the leased-pool-host
        path), we *load* the existing ``data.json`` from the host -- which the
        bake wrote with ``--template system_services --template vultr`` and
        therefore already contains the right ``name`` (always
        ``system-services``), ``command`` (``uv run bootstrap``), and
        ``additional_commands`` -- and return an agent object that mirrors
        it. The user-facing workspace name now lives on the *host* (via the
        lease's ``host_name`` field), not on the agent, so we do **not**
        rewrite ``name`` / ``command`` / ``additional_commands``.

        Two fields *are* patched when the caller supplies new values:

        - ``labels``: the bake's template-supplied defaults (e.g.
          ``is_primary=true``, ``user_created=true``) are merged with
          ``options.label_options.labels`` (caller-supplied wins on key
          collisions). This is required because the minds desktop client
          passes ``--label workspace=<workspace_name>`` for every launch
          mode, and the ``mngr forward`` / ``backend_resolver`` CEL filter
          (``has(agent.labels.workspace) && has(agent.labels.is_primary)``)
          would otherwise stop matching IMBUE_CLOUD-leased workspaces.
        - ``created_branch_name``: per-create-cycle state (which branch
          mngr created for this run), not bake state.

        ``data.json`` is only rewritten when at least one of those fields
        actually changes, so the no-op fast path stays cheap.

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

        # Hydrate the agent class from the bake's data.json so the rest of
        # mngr's create pipeline has a real ``AgentInterface`` to call
        # ``provision_agent`` / ``start_agents`` on. ``name`` / ``command`` /
        # ``additional_commands`` come straight from the bake.
        agent_type = AgentTypeName(str(existing.get("type", "command")))
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

        # Merge bake defaults with the caller's --label flags. Caller wins
        # on key collisions so a future ``mngr create --label is_primary=false``
        # would override the bake's default rather than the other way around.
        baked_labels: dict[str, str] = dict(existing.get("labels") or {})
        merged_labels: dict[str, str] = {**baked_labels, **dict(options.label_options.labels)}
        labels_changed = merged_labels != baked_labels
        branch_changed = created_branch_name is not None and existing.get("created_branch_name") != created_branch_name
        if labels_changed or branch_changed:
            patched: dict[str, Any] = dict(existing)
            if labels_changed:
                patched["labels"] = merged_labels
            if branch_changed:
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
        installed and we only need to write the agent env file so
        ``MNGR_AGENT_NAME`` / ``--env`` overrides land.

        The pre-baked agent is the system-services bootstrap process (a
        ``command``-type agent), so there is no per-agent claude config to
        patch here -- ``ANTHROPIC_API_KEY`` lives on host env and is
        inherited by the *assistant* chat agent that bootstrap creates from
        inside the container, where mngr_claude's standard provisioning
        handles the claude config setup.

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
