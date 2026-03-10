"""Core provisioning logic for injecting mng into hosts and agents."""

import importlib.metadata
import json
import shlex
import subprocess
import tempfile
from pathlib import Path
from typing import assert_never

from loguru import logger

from imbue.imbue_common.logging import log_span
from imbue.mng.config.data_types import MngContext
from imbue.mng.errors import MngError
from imbue.mng.interfaces.agent import AgentInterface
from imbue.mng.interfaces.host import OnlineHostInterface
from imbue.mng.providers.deploy_utils import MngInstallMode
from imbue.mng.providers.deploy_utils import collect_deploy_files
from imbue.mng.providers.deploy_utils import resolve_mng_install_mode
from imbue.mng_recursive.data_types import RecursivePluginConfig


def _get_remote_home(host: OnlineHostInterface) -> str:
    """Get the home directory of the default user on the remote host."""
    result = host.execute_command("echo $HOME")
    if not result.success:
        raise MngError(f"Failed to determine remote home directory: {result.stderr}")
    return result.stdout.strip()


def _resolve_remote_path(dest_path: Path, remote_home: str) -> Path:
    """Resolve a deploy destination path to an absolute path on the remote host.

    Paths starting with '~/' are resolved relative to the remote user's home.
    A bare '~' resolves to the remote home directory itself.
    Relative paths are left as-is.
    """
    dest_str = str(dest_path)
    if dest_str == "~":
        return Path(remote_home)
    if dest_str.startswith("~/"):
        return Path(remote_home) / dest_str.removeprefix("~/")
    return dest_path


def _upload_deploy_files(
    host: OnlineHostInterface,
    deploy_files: dict[Path, Path | str],
    remote_home: str,
) -> int:
    """Upload collected deploy files to the remote host.

    Returns the number of files uploaded.
    """
    count = 0
    for dest_path, source in deploy_files.items():
        resolved_path = _resolve_remote_path(dest_path, remote_home)

        # Ensure parent directory exists
        parent_str = shlex.quote(str(resolved_path.parent))
        mkdir_result = host.execute_command(f"mkdir -p {parent_str}")
        if not mkdir_result.success:
            raise MngError(f"Failed to create directory {resolved_path.parent}: {mkdir_result.stderr}")

        # Read content and upload
        if isinstance(source, Path):
            if not source.exists():
                logger.debug("Skipping non-existent deploy file: {}", source)
                continue
            content = source.read_bytes()
            host.write_file(resolved_path, content)
        else:
            host.write_text_file(resolved_path, source)

        logger.trace("Uploaded deploy file: {} -> {}", dest_path, resolved_path)
        count += 1

    return count


def _get_installed_mng_packages() -> list[tuple[str, str]]:
    """Detect which mng packages are installed locally.

    Returns a list of (package_name, version) tuples for all installed
    packages whose names start with 'mng'.
    """
    packages: list[tuple[str, str]] = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        version = dist.metadata["Version"]
        if name is not None and version is not None and (name == "mng" or name.startswith("mng-")):
            packages.append((name, version))
    return packages


def _ensure_uv_available(host: OnlineHostInterface) -> None:
    """Ensure uv is available on the host, installing it if necessary.

    After installing, verifies that uv is findable in common install locations
    ($HOME/.local/bin, $HOME/.cargo/bin). Subsequent commands that need uv
    should use _UV_PATH_PREFIX to ensure it is on the PATH.
    """
    result = host.execute_command("command -v uv")
    if result.success:
        return

    with log_span("Installing uv on host"):
        install_result = host.execute_command("curl -LsSf https://astral.sh/uv/install.sh | sh")
        if not install_result.success:
            raise MngError(f"Failed to install uv on host: {install_result.stderr.strip()}")

        # Verify uv is findable after installation. Each execute_command runs
        # in a new shell, so we need to check common install locations.
        verify_result = host.execute_command('export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" && command -v uv')
        if not verify_result.success:
            raise MngError("uv was installed but cannot be found on PATH")


def _get_mng_repo_root() -> Path:
    """Get the git repository root of the mng monorepo.

    Walks up from the mng package source to find the git repo root.
    Raises MngError if not in a git repository.
    """
    try:
        dist = importlib.metadata.distribution("mng")
    except importlib.metadata.PackageNotFoundError:
        raise MngError("mng package is not installed; cannot determine repo root") from None

    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text is None:
        raise MngError("mng is not installed in editable mode; cannot determine repo root") from None

    # Find the source directory from the editable install
    try:
        direct_url = json.loads(direct_url_text)
    except (json.JSONDecodeError, AttributeError) as e:
        raise MngError(f"Failed to parse direct_url.json for mng: {e}") from e
    url = direct_url.get("url", "")
    if url.startswith("file://"):
        source_dir = Path(url.removeprefix("file://"))
    else:
        raise MngError(f"Unexpected direct_url format: {url}") from None

    # Find git repo root from source dir
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=source_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MngError(f"Could not find git repo root from {source_dir}: {result.stderr.strip()}") from None
    return Path(result.stdout.strip())


_UV_PATH_PREFIX = 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" && '
"""Prefix for commands that need uv on the PATH after a fresh install."""


def _build_uv_env_prefix(tool_dir: Path, bin_dir: Path) -> str:
    """Build the environment variable prefix for per-agent uv tool installation.

    Sets UV_TOOL_DIR and UV_TOOL_BIN_DIR so that ``uv tool install`` places
    the tool venv and entrypoint script into agent-specific directories.
    """
    return f"export UV_TOOL_DIR={shlex.quote(str(tool_dir))} && export UV_TOOL_BIN_DIR={shlex.quote(str(bin_dir))} && "


def _install_mng_package_mode(
    host: OnlineHostInterface,
    packages: list[tuple[str, str]],
    tool_dir: Path,
    bin_dir: Path,
) -> None:
    """Install mng and plugins from PyPI using uv tool install into agent-specific directories."""
    mng_package = None
    plugin_packages: list[tuple[str, str]] = []
    for name, version in packages:
        if name == "mng":
            mng_package = (name, version)
        else:
            plugin_packages.append((name, version))

    if mng_package is None:
        raise MngError("mng package not found locally; cannot install on host")

    uv_env = _build_uv_env_prefix(tool_dir, bin_dir)
    mng_name, mng_version = mng_package
    parts = [f"uv tool install {mng_name}=={mng_version}"]
    for pkg_name, pkg_version in plugin_packages:
        parts.append(f"--with {pkg_name}=={pkg_version}")

    install_cmd = _UV_PATH_PREFIX + uv_env + " ".join(parts)
    with log_span("Installing mng (package mode)"):
        result = host.execute_command(install_cmd)
        if not result.success:
            # Try with --force-reinstall if already installed
            result = host.execute_command(install_cmd + " --force-reinstall")
            if not result.success:
                raise MngError(f"Failed to install mng: {result.stderr.strip()}")


def _install_mng_editable_mode(
    host: OnlineHostInterface,
    tool_dir: Path,
    bin_dir: Path,
) -> None:
    """Install mng from local source in editable mode.

    For local hosts, installs directly from the monorepo source tree.
    For remote hosts, packages the monorepo into a tarball, uploads it,
    extracts it, and installs in editable mode.
    """
    repo_root = _get_mng_repo_root()
    uv_env = _build_uv_env_prefix(tool_dir, bin_dir)

    if host.is_local:
        _install_mng_editable_local(host, repo_root, uv_env)
    else:
        _install_mng_editable_remote(host, repo_root, uv_env)


def _install_mng_editable_local(
    host: OnlineHostInterface,
    repo_root: Path,
    uv_env: str,
) -> None:
    """Install mng in editable mode on a local host by pointing directly at the source tree."""
    quoted_root = shlex.quote(str(repo_root))

    # Discover which mng plugin libs exist in the repo
    libs_dir = repo_root / "libs"
    lib_names = [d.name for d in libs_dir.iterdir() if d.is_dir()] if libs_dir.is_dir() else []

    install_parts = [f"{_UV_PATH_PREFIX}{uv_env}cd {quoted_root} && uv tool install -e libs/mng"]
    for lib_name in lib_names:
        if lib_name != "mng" and lib_name.startswith("mng_"):
            install_parts.append(f"--with-editable libs/{lib_name}")

    install_cmd = " ".join(install_parts)
    with log_span("Installing mng (editable mode, local)"):
        result = host.execute_command(install_cmd)
        if not result.success:
            result = host.execute_command(install_cmd + " --force-reinstall")
            if not result.success:
                raise MngError(f"Failed to install mng in editable mode: {result.stderr.strip()}")


def _install_mng_editable_remote(
    host: OnlineHostInterface,
    repo_root: Path,
    uv_env: str,
) -> None:
    """Install mng in editable mode on a remote host by uploading a tarball."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tarball_path = Path(tmpdir) / "mng-repo.tar.gz"

        # Create tarball of the monorepo using git archive
        with log_span("Packaging mng monorepo for transfer"):
            result = subprocess.run(
                ["git", "archive", "--format=tar.gz", "-o", str(tarball_path), "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise MngError(f"Failed to create mng monorepo tarball: {result.stderr.strip()}")

        # Upload tarball to remote host
        remote_tarball = Path("/tmp/mng-repo.tar.gz")
        remote_repo_dir = Path("/tmp/mng-repo")

        with log_span("Uploading mng monorepo to remote host"):
            tarball_content = tarball_path.read_bytes()
            host.write_file(remote_tarball, tarball_content)

        # Extract and install on remote
        with log_span("Installing mng (editable mode, remote)"):
            extract_cmd = f"rm -rf {remote_repo_dir} && mkdir -p {remote_repo_dir} && tar -xzf {remote_tarball} -C {remote_repo_dir} && rm {remote_tarball}"
            result = host.execute_command(extract_cmd)
            if not result.success:
                raise MngError(f"Failed to extract mng tarball: {result.stderr.strip()}")

            # Build the install command with editable installs for all workspace packages
            # First, discover which libs exist in the tarball
            ls_result = host.execute_command(f"ls {remote_repo_dir}/libs/")
            if not ls_result.success:
                raise MngError(f"Failed to list mng libs: {ls_result.stderr.strip()}")

            lib_names = ls_result.stdout.strip().split()
            install_parts = [f"{_UV_PATH_PREFIX}{uv_env}cd {remote_repo_dir} && uv tool install -e libs/mng"]
            for lib_name in lib_names:
                if lib_name != "mng" and lib_name.startswith("mng_"):
                    install_parts.append(f"--with-editable libs/{lib_name}")

            install_cmd = " ".join(install_parts)
            result = host.execute_command(install_cmd)
            if not result.success:
                # Try with --force-reinstall
                result = host.execute_command(install_cmd + " --force-reinstall")
                if not result.success:
                    raise MngError(f"Failed to install mng in editable mode: {result.stderr.strip()}")


def _get_agent_state_dir(agent: AgentInterface, host: OnlineHostInterface) -> Path:
    """Get the agent's state directory path.

    Mirrors the convention in host.py:_get_agent_state_dir and
    base_agent.py:_get_agent_dir.
    """
    return host.host_dir / "agents" / str(agent.id)


def provision_mng_on_host(
    host: OnlineHostInterface,
    mng_ctx: MngContext,
) -> None:
    """Provision host-level mng prerequisites (deploy files, uv availability).

    For remote hosts: uploads config files and ensures uv is installed.
    For local hosts: ensures uv is available.

    The actual mng installation is done per-agent by provision_mng_for_agent().
    """
    plugin_config = mng_ctx.get_plugin_config("recursive", RecursivePluginConfig)

    resolved_mode = resolve_mng_install_mode(plugin_config.install_mode)
    if resolved_mode == MngInstallMode.SKIP:
        logger.debug("Skipping mng provisioning (install_mode=skip)")
        return

    try:
        with log_span("Provisioning mng prerequisites on host"):
            if not host.is_local:
                # Get the remote user's home directory
                remote_home = _get_remote_home(host)

                # Collect and upload deploy files.
                repo_root = Path.cwd()
                try:
                    deploy_files = collect_deploy_files(
                        mng_ctx=mng_ctx,
                        repo_root=repo_root,
                        include_user_settings=True,
                        include_project_settings=True,
                    )
                except Exception as e:
                    raise MngError(f"Failed to collect deploy files: {e}") from e

                if deploy_files:
                    with log_span("Uploading {} deploy files to remote host", len(deploy_files)):
                        uploaded = _upload_deploy_files(host, deploy_files, remote_home)
                        logger.info("Uploaded {} mng config files to remote host", uploaded)

            # Ensure uv is available on the host
            _ensure_uv_available(host)

    except MngError as e:
        if plugin_config.is_errors_fatal:
            raise
        logger.warning("Failed to provision mng prerequisites on host: {}", e)


def provision_mng_for_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mng_ctx: MngContext,
) -> None:
    """Install mng into the agent's state directory.

    Installs mng using ``uv tool install`` with ``UV_TOOL_DIR`` and
    ``UV_TOOL_BIN_DIR`` set to per-agent directories, so each agent gets
    its own isolated mng installation:

    - ``<agent_state_dir>/tools/``  -- tool venv (UV_TOOL_DIR)
    - ``<agent_state_dir>/bin/``    -- entrypoint script (UV_TOOL_BIN_DIR)

    This ensures multiple agents on the same host (even local) can each
    have their own mng version without conflicts.
    """
    plugin_config = mng_ctx.get_plugin_config("recursive", RecursivePluginConfig)

    resolved_mode = resolve_mng_install_mode(plugin_config.install_mode)
    if resolved_mode == MngInstallMode.SKIP:
        logger.debug("Skipping per-agent mng installation (install_mode=skip)")
        return

    agent_state_dir = _get_agent_state_dir(agent, host)
    tool_dir = agent_state_dir / "tools"
    bin_dir = agent_state_dir / "bin"

    try:
        with log_span("Installing mng for agent '{}' into {}", agent.name, agent_state_dir):
            # Create the target directories
            for d in (tool_dir, bin_dir):
                mkdir_result = host.execute_command(f"mkdir -p {shlex.quote(str(d))}")
                if not mkdir_result.success:
                    raise MngError(f"Failed to create directory {d}: {mkdir_result.stderr}")

            match resolved_mode:
                case MngInstallMode.PACKAGE:
                    packages = _get_installed_mng_packages()
                    if packages:
                        _install_mng_package_mode(host, packages, tool_dir, bin_dir)
                    else:
                        logger.warning("No mng packages found locally; cannot install for agent")
                case MngInstallMode.EDITABLE:
                    _install_mng_editable_mode(host, tool_dir, bin_dir)
                case MngInstallMode.SKIP:
                    pass
                case MngInstallMode.AUTO:
                    raise MngError(f"Unexpected unresolved install mode: {resolved_mode}")
                case _ as unreachable:
                    assert_never(unreachable)

    except MngError as e:
        if plugin_config.is_errors_fatal:
            raise
        logger.warning("Failed to install mng for agent '{}': {}", agent.name, e)
