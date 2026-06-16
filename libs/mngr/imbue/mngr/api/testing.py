"""Shared test fixtures for API tests."""

import shlex
import shutil
import subprocess
import types
from collections.abc import Generator
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.agents.base_agent import BaseAgent
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.api.git import LocalGitContext
from imbue.mngr.api.observe import AgentObserver
from imbue.mngr.api.observe import get_default_events_base_dir
from imbue.mngr.api.providers import _instance_cache
from imbue.mngr.config.data_types import AgentTypeConfig
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.interfaces.provider_instance import ProviderInstanceInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostName
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.base_provider import BaseProviderInstance


@contextmanager
def created_host(
    provider: ProviderInstanceInterface,
    host_name: HostName,
    **create_kwargs: Any,
) -> Iterator[OnlineHostInterface]:
    """Create a host via ``provider`` and destroy it on exit.

    Replaces the create-host / ``try``-``finally``-``destroy_host`` boilerplate that
    recurs across provider tests (modal, docker, ssh) with a single ``with`` block.
    Extra keyword arguments are forwarded to ``provider.create_host``.
    """
    host = provider.create_host(host_name, **create_kwargs)
    try:
        yield host
    finally:
        provider.destroy_host(host)


class FakeAgent(FrozenModel):
    """Minimal test double for AgentInterface -- only implements work_dir and name."""

    work_dir: Path = Field(description="Working directory for this agent")
    name: AgentName = Field(default=AgentName("fake-agent"), description="Agent name")


class FakeHost(MutableModel):
    """Minimal test double for OnlineHostInterface that executes commands locally."""

    is_local: bool = Field(default=True, description="Whether this is a local host")
    host_dir: Path = Field(default_factory=lambda: Path("/fake/host_dir"), description="Host state directory")
    ssh_info: tuple[str, str, int, Path] | None = Field(
        default=None,
        description="SSH connection info (user, hostname, port, key_path) for remote hosts",
    )
    ssh_known_hosts_file: str | None = Field(
        default=None,
        description="Path to known_hosts file for SSH host key verification",
    )

    @property
    def connector(self) -> types.SimpleNamespace:
        """Provide a connector-like attribute with host data for SSH configuration."""
        data: dict[str, str] = {}
        if self.ssh_known_hosts_file is not None:
            data["ssh_known_hosts_file"] = self.ssh_known_hosts_file
        return types.SimpleNamespace(host=types.SimpleNamespace(data=data))

    def _execute_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute a command locally and return the result.

        The user, env, and timeout_seconds parameters are accepted for interface
        compatibility but are not applied to the subprocess call.
        """
        result = subprocess.run(
            shlex.split(command),
            capture_output=True,
            text=True,
            cwd=cwd,
        )
        return CommandResult(
            stdout=result.stdout,
            stderr=result.stderr,
            success=result.returncode == 0,
        )

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute an idempotent command locally."""
        return self._execute_command(command, user=user, cwd=cwd, env=env, timeout_seconds=timeout_seconds)

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Execute a stateful command locally."""
        return self._execute_command(command, user=user, cwd=cwd, env=env, timeout_seconds=timeout_seconds)

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        """Read a file from the local filesystem."""
        return path.read_text(encoding=encoding)

    def write_text_file(self, path: Path, content: str, encoding: str = "utf-8", mode: str | None = None) -> None:
        """Write a text file to the local filesystem."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding=encoding)

    def write_file(self, path: Path, content: bytes, mode: str | None = None) -> None:
        """Write a binary file to the local filesystem."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def copy_directory(
        self,
        source_host: object,
        source_path: Path,
        target_path: Path,
        extra_args: str | None = None,
        exclude_git: bool = False,
    ) -> None:
        """Copy a directory using local filesystem operations.

        FakeHost always operates on the local filesystem, so this uses
        shutil.copytree regardless of the is_local flag.
        """
        target_path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_path, target_path, dirs_exist_ok=True)

    def copy_local_directory(self, source_path: Path, target_path: Path, extra_args: str | None = None) -> None:
        """Merge a local directory into target_path file-by-file (mimics rsync -r, additive).

        Unlike a plain copytree, this handles a ``target_path`` of "/" (the real
        upload path stages absolute remote paths and rsyncs to the filesystem root):
        each staged file lands at ``target_path / <relative>``, which reconstructs the
        intended absolute path. Ignores ``extra_args`` (include/exclude filters), like
        ``copy_directory``.
        """
        for staged in Path(source_path).rglob("*"):
            if staged.is_file():
                dest = target_path / staged.relative_to(source_path)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(staged, dest)

    def get_ssh_connection_info(self) -> tuple[str, str, int, Path] | None:
        """Return configured SSH connection info, or None for local hosts."""
        if self.is_local:
            return None
        return self.ssh_info

    def get_env_var(self, key: str) -> str | None:
        """No-op env-var lookup. Tests that need env-var introspection should use a richer fake."""
        del key
        return None

    def get_env_vars(self) -> dict[str, str]:
        """No-op env-var dump. Tests that need env-var introspection should use a richer fake."""
        return {}


class SyncTestContext(FrozenModel):
    """Shared test context for sync integration tests (pull, push, pair)."""

    agent_dir: Path = Field(description="Agent working directory")
    local_dir: Path = Field(description="Local directory")
    agent: Any = Field(description="Test agent (FakeAgent)")
    host: Any = Field(description="Test host (FakeHost)")


def has_uncommitted_changes(path: Path, cg: ConcurrencyGroup) -> bool:
    """Check for uncommitted changes using LocalGitContext."""
    return LocalGitContext(cg=cg).has_uncommitted_changes(path)


class FixedProcessNameAgent(BaseAgent):
    """A ``BaseAgent`` whose ``get_expected_process_name`` returns a fixed value.

    ``BaseAgent.get_expected_process_name`` reads data.json via the host connector,
    which fails for SSH hosts in tests (no SSH server is running). This double
    short-circuits that path so connect/disconnect tests can run against a remote
    host without provisioning real agent state.

    Named without a ``Test`` prefix so pytest does not attempt to collect it.
    """

    def get_expected_process_name(self) -> str:
        return "test-process"


def make_fixed_process_name_agent(
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
    agent_name: str = "test-agent",
) -> FixedProcessNameAgent:
    """Build a ``FixedProcessNameAgent`` running on ``host``."""
    return FixedProcessNameAgent(
        id=AgentId(f"agent-{uuid4().hex}"),
        name=AgentName(agent_name),
        agent_type=AgentTypeName("generic"),
        work_dir=Path("/tmp/work"),
        create_time=datetime.now(timezone.utc),
        host_id=host.id,
        mngr_ctx=mngr_ctx,
        agent_config=AgentTypeConfig(),
        host=host,
    )


def make_test_agent_observer(mngr_ctx: MngrContext, mngr_binary: str) -> AgentObserver:
    """Create an ``AgentObserver`` with events_base_dir derived from the test config.

    ``mngr_binary`` should be a no-op binary that accepts any arguments (e.g. the
    ``noop_binary`` fixture), since the observer shells out to it.
    """
    return AgentObserver(
        mngr_ctx=mngr_ctx,
        events_base_dir=get_default_events_base_dir(mngr_ctx.config),
        mngr_binary=mngr_binary,
    )


def make_test_provider(name: str, backend: str = "docker") -> DiscoveredProvider:
    """Create a ``DiscoveredProvider`` with the given name and (enabled) backend."""
    return make_discovered_provider(
        ProviderInstanceName(name),
        ProviderInstanceConfig(backend=ProviderBackendName(backend), is_enabled=True),
    )


@contextmanager
def inject_provider_instance(
    provider: BaseProviderInstance,
    mngr_ctx: MngrContext,
) -> Generator[None, None, None]:
    """Temporarily inject ``provider`` into the provider-instance cache that
    ``get_provider_instance`` (and thus ``resolve_target_host`` / cleanup) reads.

    The cache is keyed by ``(provider.name, id(mngr_ctx))``, so the same
    ``mngr_ctx`` object must be passed both here and to the code under test.
    Any prior cache entry for the same key is saved and restored on exit, and the
    injected entry is removed, so nothing leaks into other tests.
    """
    cache_key = (provider.name, id(mngr_ctx))
    had_prior = cache_key in _instance_cache
    prior_instance = _instance_cache.get(cache_key)
    _instance_cache[cache_key] = provider
    try:
        yield
    finally:
        if had_prior:
            assert prior_instance is not None
            _instance_cache[cache_key] = prior_instance
        else:
            _instance_cache.pop(cache_key, None)
