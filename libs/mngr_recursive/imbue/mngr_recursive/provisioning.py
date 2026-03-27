"""Core provisioning logic for injecting mngr into hosts and agents."""

import importlib.metadata
import json
import shlex
import subprocess
import tempfile
from concurrent.futures import Future
from pathlib import Path
from typing import assert_never

from loguru import logger

from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.logging import log_span
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import MngrError
from imbue.mngr.interfaces.agent import AgentInterface
from imbue.mngr.interfaces.host import OnlineHostInterface
from imbue.mngr.providers.deploy_utils import MngrInstallMode
from imbue.mngr.providers.deploy_utils import collect_deploy_files
from imbue.mngr.providers.deploy_utils import resolve_mngr_install_mode
from imbue.mngr_recursive.data_types import RecursivePluginConfig


def _get_remote_home(host: OnlineHostInterface) -> str:
    """Get the home directory of the default user on the remote host."""
    result = host.execute_idempotent_command("echo $HOME")
    if not result.success:
        raise MngrError(f"Failed to determine remote home directory: {result.stderr}")
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
    mngr_ctx: MngrContext,
) -> int:
    """Upload collected deploy files to the remote host.

    Returns the number of files uploaded.
    """
    # do this in parallel, since there can sometimes be a bunch of things to transfer
    # first, figure out all directories and do a single mkdir -p that captures all of them:
    remote_paths: list[str] = []
    for dest_path in deploy_files:
        resolved_path = _resolve_remote_path(dest_path, remote_home)
        remote_paths.append(shlex.quote(str(resolved_path.parent)))
    mkdir_result = host.execute_idempotent_command(f"mkdir -p {' '.join(remote_paths)}")
    if not mkdir_result.success:
        raise MngrError(f"Failed to create directories: {mkdir_result.stderr}")

    # then upload them all in parallel
    count = 0
    futures: list[Future[None]] = []
    with ConcurrencyGroupExecutor(
        parent_cg=mngr_ctx.concurrency_group, name="upload_deploy_files", max_workers=16
    ) as executor:
        for dest_path, source in deploy_files.items():
            resolved_path = _resolve_remote_path(dest_path, remote_home)

            # Read content and upload
            if isinstance(source, Path):
                if not source.exists():
                    logger.debug("Skipping non-existent deploy file: {}", source)
                    continue
                content = source.read_bytes()
                futures.append(executor.submit(host.write_file, path=resolved_path, content=content))
            else:
                futures.append(executor.submit(host.write_text_file, path=resolved_path, content=source))

            logger.trace("Uploaded deploy file: {} -> {}", dest_path, resolved_path)
            count += 1

    # Re-raise any thread exceptions (e.g. abort-mode errors)
    for future in futures:
        future.result()

    return count


def _get_installed_mngr_packages() -> list[tuple[str, str]]:
    """Detect which mngr packages are installed locally.

    Returns a list of (package_name, version) tuples for all installed
    packages whose names start with 'mngr'.
    """
    packages: list[tuple[str, str]] = []
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        version = dist.metadata["Version"]
        if name is not None and version is not None and (name == "imbue-mngr" or name.startswith("imbue-mngr-")):
            packages.append((name, version))
    return packages


def _ensure_uv_available(host: OnlineHostInterface) -> None:
    """Ensure uv is available on the host, installing it if necessary.

    After installing, verifies that uv is findable in common install locations
    ($HOME/.local/bin, $HOME/.cargo/bin). Subsequent commands that need uv
    should use _UV_PATH_PREFIX to ensure it is on the PATH.
    """
    result = host.execute_idempotent_command("command -v uv")
    if result.success:
        return

    with log_span("Installing uv on host"):
        install_result = host.execute_idempotent_command("curl -LsSf https://astral.sh/uv/install.sh | sh")
        if not install_result.success:
            raise MngrError(f"Failed to install uv on host: {install_result.stderr.strip()}")

        # Verify uv is findable after installation. Each execute_command runs
        # in a new shell, so we need to check common install locations.
        verify_result = host.execute_idempotent_command('export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" && command -v uv')
        if not verify_result.success:
            raise MngrError("uv was installed but cannot be found on PATH")


def _get_mngr_repo_root() -> Path:
    """Get the git repository root of the mngr monorepo.

    Walks up from the mngr package source to find the git repo root.
    Raises MngrError if not in a git repository.
    """
    try:
        dist = importlib.metadata.distribution("imbue-mngr")
    except importlib.metadata.PackageNotFoundError:
        raise MngrError("mngr package is not installed; cannot determine repo root") from None

    direct_url_text = dist.read_text("direct_url.json")
    if direct_url_text is None:
        raise MngrError("mngr is not installed in editable mode; cannot determine repo root") from None

    # Find the source directory from the editable install
    try:
        direct_url = json.loads(direct_url_text)
    except (json.JSONDecodeError, AttributeError) as e:
        raise MngrError(f"Failed to parse direct_url.json for mngr: {e}") from e
    url = direct_url.get("url", "")
    if url.startswith("file://"):
        source_dir = Path(url.removeprefix("file://"))
    else:
        raise MngrError(f"Unexpected direct_url format: {url}") from None

    # Find git repo root from source dir
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=source_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MngrError(f"Could not find git repo root from {source_dir}: {result.stderr.strip()}") from None
    return Path(result.stdout.strip())


_UV_PATH_PREFIX = 'export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH" && '
"""Prefix for commands that need uv on the PATH after a fresh install."""


def _build_uv_env_prefix(tool_dir: Path, bin_dir: Path) -> str:
    """Build the environment variable prefix for per-agent uv tool installation.

    Sets UV_TOOL_DIR and UV_TOOL_BIN_DIR so that ``uv tool install`` places
    the tool venv and entrypoint script into agent-specific directories.
    """
    return f"export UV_TOOL_DIR={shlex.quote(str(tool_dir))} && export UV_TOOL_BIN_DIR={shlex.quote(str(bin_dir))} && "


def _install_mngr_package_mode(
    host: OnlineHostInterface,
    packages: list[tuple[str, str]],
    tool_dir: Path,
    bin_dir: Path,
) -> None:
    """Install mngr and plugins from PyPI using uv tool install into agent-specific directories."""
    mngr_package = None
    plugin_packages: list[tuple[str, str]] = []
    for name, version in packages:
        if name == "imbue-mngr":
            mngr_package = (name, version)
        else:
            plugin_packages.append((name, version))

    if mngr_package is None:
        raise MngrError("mngr package not found locally; cannot install on host")

    uv_env = _build_uv_env_prefix(tool_dir, bin_dir)
    mngr_name, mngr_version = mngr_package
    parts = [f"uv tool install {mngr_name}=={mngr_version}"]
    for pkg_name, pkg_version in plugin_packages:
        parts.append(f"--with {pkg_name}=={pkg_version}")

    install_cmd = _UV_PATH_PREFIX + uv_env + " ".join(parts)
    with log_span("Installing mngr (package mode)"):
        result = host.execute_idempotent_command(install_cmd)
        if not result.success:
            # Try with --force-reinstall if already installed
            result = host.execute_idempotent_command(install_cmd + " --force-reinstall")
            if not result.success:
                raise MngrError(f"Failed to install mngr: {result.stderr.strip()}")


def _install_mngr_editable_mode(
    host: OnlineHostInterface,
    tool_dir: Path,
    bin_dir: Path,
) -> None:
    """Install mngr from local source in editable mode.

    For local hosts, installs directly from the monorepo source tree.
    For remote hosts, packages the monorepo into a tarball, uploads it,
    extracts it, and installs in editable mode.
    """
    repo_root = _get_mngr_repo_root()
    uv_env = _build_uv_env_prefix(tool_dir, bin_dir)

    if host.is_local:
        _install_mngr_editable_local(host, repo_root, uv_env)
    else:
        _install_mngr_editable_remote(host, repo_root, uv_env)


def _install_mngr_editable_local(
    host: OnlineHostInterface,
    repo_root: Path,
    uv_env: str,
) -> None:
    """Install mngr in editable mode on a local host by pointing directly at the source tree."""
    quoted_root = shlex.quote(str(repo_root))

    # Discover which mngr plugin libs exist in the repo
    libs_dir = repo_root / "libs"
    lib_names = [d.name for d in libs_dir.iterdir() if d.is_dir()] if libs_dir.is_dir() else []

    install_parts = [f"{_UV_PATH_PREFIX}{uv_env}cd {quoted_root} && uv tool install -e libs/mngr"]
    for lib_name in lib_names:
        if lib_name != "imbue-mngr" and lib_name.startswith("mngr_"):
            install_parts.append(f"--with-editable libs/{lib_name}")

    install_cmd = " ".join(install_parts)
    with log_span("Installing mngr (editable mode, local)"):
        result = host.execute_idempotent_command(install_cmd)
        if not result.success:
            result = host.execute_idempotent_command(install_cmd + " --force-reinstall")
            if not result.success:
                raise MngrError(f"Failed to install mngr in editable mode: {result.stderr.strip()}")


def _install_mngr_editable_remote(
    host: OnlineHostInterface,
    repo_root: Path,
    uv_env: str,
) -> None:
    """Install mngr in editable mode on a remote host by uploading a tarball."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tarball_path = Path(tmpdir) / "mngr-repo.tar.gz"

        # Create tarball of the monorepo using git archive
        with log_span("Packaging mngr monorepo for transfer"):
            result = subprocess.run(
                ["git", "archive", "--format=tar.gz", "-o", str(tarball_path), "HEAD"],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise MngrError(f"Failed to create mngr monorepo tarball: {result.stderr.strip()}")

        # Upload tarball to remote host
        remote_tarball = Path("/tmp/mngr-repo.tar.gz")
        remote_repo_dir = Path("/tmp/mngr-repo")

        with log_span("Uploading mngr monorepo to remote host"):
            tarball_content = tarball_path.read_bytes()
            host.write_file(remote_tarball, tarball_content)

        # Extract and install on remote
        with log_span("Installing mngr (editable mode, remote)"):
            extract_cmd = f"rm -rf {remote_repo_dir} && mkdir -p {remote_repo_dir} && tar -xzf {remote_tarball} -C {remote_repo_dir} && rm {remote_tarball}"
            result = host.execute_idempotent_command(extract_cmd)
            if not result.success:
                raise MngrError(f"Failed to extract mngr tarball: {result.stderr.strip()}")

            # Build the install command with editable installs for all workspace packages
            # First, discover which libs exist in the tarball
            ls_result = host.execute_idempotent_command(f"ls {remote_repo_dir}/libs/")
            if not ls_result.success:
                raise MngrError(f"Failed to list mngr libs: {ls_result.stderr.strip()}")

            lib_names = ls_result.stdout.strip().split()
            install_parts = [f"{_UV_PATH_PREFIX}{uv_env}cd {remote_repo_dir} && uv tool install -e libs/mngr"]
            for lib_name in lib_names:
                if lib_name != "imbue-mngr" and lib_name.startswith("mngr_"):
                    install_parts.append(f"--with-editable libs/{lib_name}")

            install_cmd = " ".join(install_parts)
            result = host.execute_idempotent_command(install_cmd)
            if not result.success:
                # Try with --force-reinstall
                result = host.execute_idempotent_command(install_cmd + " --force-reinstall")
                if not result.success:
                    raise MngrError(f"Failed to install mngr in editable mode: {result.stderr.strip()}")


def _get_agent_state_dir(agent: AgentInterface, host: OnlineHostInterface) -> Path:
    """Get the agent's state directory path.

    Mirrors the convention in host.py:_get_agent_state_dir and
    base_agent.py:_get_agent_dir.
    """
    return host.host_dir / "agents" / str(agent.id)


def provision_mngr_on_host(
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
) -> None:
    """Provision host-level mngr prerequisites (deploy files, uv availability).

    For remote hosts: uploads config files and ensures uv is installed.
    For local hosts: ensures uv is available.

    The actual mngr installation is done per-agent by provision_mngr_for_agent().
    """
    plugin_config = mngr_ctx.get_plugin_config("recursive", RecursivePluginConfig)

    resolved_mode = resolve_mngr_install_mode(plugin_config.install_mode)
    if resolved_mode == MngrInstallMode.SKIP:
        logger.debug("Skipping mngr provisioning (install_mode=skip)")
        return

    try:
        with log_span("Provisioning mngr prerequisites on host"):
            if not host.is_local:
                # Get the remote user's home directory
                remote_home = _get_remote_home(host)

                # Collect and upload deploy files.
                repo_root = Path.cwd()
                try:
                    deploy_files = collect_deploy_files(
                        mngr_ctx=mngr_ctx,
                        repo_root=repo_root,
                        include_user_settings=True,
                        include_project_settings=True,
                    )
                except Exception as e:
                    raise MngrError(f"Failed to collect deploy files: {e}") from e

                if deploy_files:
                    with log_span("Uploading {} deploy files to remote host", len(deploy_files)):
                        uploaded = _upload_deploy_files(host, deploy_files, remote_home, mngr_ctx)
                        logger.info("Uploaded {} mngr config files to remote host", uploaded)

            # Ensure uv is available on the host
            _ensure_uv_available(host)

    except MngrError as e:
        if plugin_config.is_errors_fatal:
            raise
        logger.warning("Failed to provision mngr prerequisites on host: {}", e)


def provision_mngr_for_agent(
    agent: AgentInterface,
    host: OnlineHostInterface,
    mngr_ctx: MngrContext,
) -> None:
    """Install mngr into the agent's state directory.

    Installs mngr using ``uv tool install`` with ``UV_TOOL_DIR`` and
    ``UV_TOOL_BIN_DIR`` set to per-agent directories, so each agent gets
    its own isolated mngr installation:

    - ``<agent_state_dir>/tools/``  -- tool venv (UV_TOOL_DIR)
    - ``<agent_state_dir>/bin/``    -- entrypoint script (UV_TOOL_BIN_DIR)

    This ensures multiple agents on the same host (even local) can each
    have their own mngr version without conflicts.
    """
    plugin_config = mngr_ctx.get_plugin_config("recursive", RecursivePluginConfig)

    resolved_mode = resolve_mngr_install_mode(plugin_config.install_mode)
    if resolved_mode == MngrInstallMode.SKIP:
        logger.debug("Skipping per-agent mngr installation (install_mode=skip)")
        return

    agent_state_dir = _get_agent_state_dir(agent, host)
    tool_dir = agent_state_dir / "tools"
    bin_dir = agent_state_dir / "bin"

    try:
        with log_span("Installing mngr for agent '{}' into {}", agent.name, agent_state_dir):
            # Create the target directories
            for d in (tool_dir, bin_dir):
                mkdir_result = host.execute_idempotent_command(f"mkdir -p {shlex.quote(str(d))}")
                if not mkdir_result.success:
                    raise MngrError(f"Failed to create directory {d}: {mkdir_result.stderr}")

            match resolved_mode:
                case MngrInstallMode.PACKAGE:
                    packages = _get_installed_mngr_packages()
                    if packages:
                        _install_mngr_package_mode(host, packages, tool_dir, bin_dir)
                    else:
                        logger.warning("No mngr packages found locally; cannot install for agent")
                case MngrInstallMode.EDITABLE:
                    _install_mngr_editable_mode(host, tool_dir, bin_dir)
                case MngrInstallMode.SKIP:
                    pass
                case MngrInstallMode.AUTO:
                    raise MngrError(f"Unexpected unresolved install mode: {resolved_mode}")
                case _ as unreachable:
                    assert_never(unreachable)

    except MngrError as e:
        if plugin_config.is_errors_fatal:
            raise
        logger.warning("Failed to install mngr for agent '{}': {}", agent.name, e)
