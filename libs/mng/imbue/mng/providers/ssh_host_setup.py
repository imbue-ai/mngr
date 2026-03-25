import importlib.resources
from pathlib import Path
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mng import resources

# Prefix used in shell output to identify warnings that should be shown to the user
WARNING_PREFIX: Final[str] = "MNG_WARN:"


class RequiredHostPackage(FrozenModel):
    """An apt package that must be present on remote hosts for mng to function."""

    package: str = Field(description="Apt package name (e.g. 'openssh-server')")
    binary: str = Field(description="Binary name used when checking whether the package is installed")
    check_cmd: str | None = Field(
        default=None,
        description="Custom shell command to check for the package, or None to use 'command -v <binary>'",
    )


# Packages that must be present on any remote host for mng to function.
# Providers that build a default image should pre-install these; the runtime
# check in build_check_and_install_packages_command will still install any
# that are missing (with a warning).
REQUIRED_HOST_PACKAGES: Final[tuple[RequiredHostPackage, ...]] = (
    RequiredHostPackage(
        package="ca-certificates",
        binary="update-ca-certificates",
        check_cmd="test -f /etc/ssl/certs/ca-certificates.crt",
    ),
    RequiredHostPackage(package="openssh-server", binary="sshd", check_cmd="test -x /usr/sbin/sshd"),
    RequiredHostPackage(package="tmux", binary="tmux"),
    RequiredHostPackage(package="curl", binary="curl"),
    RequiredHostPackage(package="rsync", binary="rsync"),
    RequiredHostPackage(package="git", binary="git"),
    RequiredHostPackage(package="jq", binary="jq"),
    RequiredHostPackage(package="xxd", binary="xxd"),
)


@pure
def get_user_ssh_dir(user: str) -> Path:
    """Get the SSH directory path for a given user.

    Returns /root/.ssh for root, /home/<user>/.ssh for others.
    """
    if user == "root":
        return Path("/root/.ssh")
    else:
        return Path(f"/home/{user}/.ssh")


@pure
def _build_package_check_snippet(pkg: RequiredHostPackage) -> str:
    """Build a shell snippet that checks for a package and adds it to the install list."""
    check = pkg.check_cmd if pkg.check_cmd is not None else f"command -v {pkg.binary} >/dev/null 2>&1"
    return (
        f"if ! {check}; then "
        f"echo '{WARNING_PREFIX}{pkg.package} is not pre-installed in the base image. "
        f"Installing at runtime. For faster startup, consider using an image with {pkg.package} pre-installed.'; "
        f'PKGS_TO_INSTALL="$PKGS_TO_INSTALL {pkg.package}"; '
        "fi"
    )


@pure
def build_check_and_install_packages_command(
    mng_host_dir: str,
    host_volume_mount_path: str | None = None,
) -> str:
    """Build a single shell command that checks for and installs required packages.

    This command:
    1. Checks for each required package (ca-certificates, sshd, tmux, curl, rsync, git, jq, xxd)
    2. Echoes a prefixed warning for each missing package
    3. Installs all missing packages in a single apt-get call
    4. Creates the sshd run directory (/run/sshd)
    5. Sets up the mng host directory (either via mkdir or symlink to volume)

    When host_volume_mount_path is provided, the host directory is created as
    a symlink to the volume mount path instead of as a regular directory. This
    causes all data written to host_dir to persist on the volume.

    Returns a shell command string that can be executed via sh -c.
    """
    script_lines = [
        "PKGS_TO_INSTALL=''",
        *(_build_package_check_snippet(pkg) for pkg in REQUIRED_HOST_PACKAGES),
        # Install missing packages if any
        'if [ -n "$PKGS_TO_INSTALL" ]; then apt-get update -qq && apt-get install -y -qq $PKGS_TO_INSTALL; fi',
        # Create sshd run directory (required for sshd to start)
        "mkdir -p /run/sshd",
    ]

    if host_volume_mount_path is not None:
        # Remove any existing directory (e.g., from a pre-volume snapshot) before
        # creating the symlink. ln -sfn alone won't replace an existing directory.
        # The subshell groups the conditional removal so && chaining works correctly.
        script_lines.append(f"( [ -L {mng_host_dir} ] || rm -rf {mng_host_dir} )")
        script_lines.append(f"ln -sfn {host_volume_mount_path} {mng_host_dir}")
    else:
        script_lines.append(f"mkdir -p {mng_host_dir}")

    return " && ".join(script_lines)


@pure
def build_configure_ssh_command(
    user: str,
    client_public_key: str,
    host_private_key: str,
    host_public_key: str,
) -> str:
    """Build a shell command that configures SSH keys and permissions.

    This command:
    1. Creates the user's .ssh directory
    2. Writes the authorized_keys file (for client authentication)
    3. Removes any existing host keys
    4. Installs the provided host key (for host identification)
    5. Sets correct permissions on all files

    Returns a shell command string that can be executed via sh -c.
    """
    ssh_dir = get_user_ssh_dir(user)
    authorized_keys_path = ssh_dir / "authorized_keys"

    # Escape single quotes in keys by ending the quote, adding escaped quote, starting quote again
    # e.g., key'with'quotes becomes key'\''with'\''quotes
    escaped_client_key = client_public_key.replace("'", "'\"'\"'")
    escaped_host_private_key = host_private_key.replace("'", "'\"'\"'")
    escaped_host_public_key = host_public_key.replace("'", "'\"'\"'")

    script_lines = [
        # Create .ssh directory
        f"mkdir -p '{ssh_dir}'",
        # Write authorized_keys file
        f"printf '%s\\n' '{escaped_client_key}' > '{authorized_keys_path}'",
        # Set permissions on authorized_keys
        f"chmod 600 '{authorized_keys_path}'",
        # Remove any existing host keys (important for restored sandboxes)
        "rm -f /etc/ssh/ssh_host_*",
        # Write the host private key
        f"printf '%s' '{escaped_host_private_key}' > /etc/ssh/ssh_host_ed25519_key",
        # Write the host public key
        f"printf '%s' '{escaped_host_public_key}' > /etc/ssh/ssh_host_ed25519_key.pub",
        # Set correct permissions on host keys
        "chmod 600 /etc/ssh/ssh_host_ed25519_key",
        "chmod 644 /etc/ssh/ssh_host_ed25519_key.pub",
    ]

    return " && ".join(script_lines)


@pure
def build_add_known_hosts_command(
    user: str,
    known_hosts_entries: tuple[str, ...],
) -> str | None:
    """Build a shell command that adds entries to the user's known_hosts file.

    This command:
    1. Creates the user's .ssh directory if it doesn't exist
    2. Appends each known_hosts entry to the known_hosts file

    Each entry should be a full known_hosts line (e.g., "github.com ssh-rsa AAAA...")

    Returns a shell command string that can be executed via sh -c, or None if
    there are no entries to add.
    """
    if not known_hosts_entries:
        return None

    ssh_dir = get_user_ssh_dir(user)
    known_hosts_path = ssh_dir / "known_hosts"

    script_lines: list[str] = [
        # Create .ssh directory if needed
        f"mkdir -p '{ssh_dir}'",
    ]

    for entry in known_hosts_entries:
        # Escape single quotes in the entry
        escaped_entry = entry.replace("'", "'\"'\"'")
        # Append entry to known_hosts (with a newline)
        script_lines.append(f"printf '%s\\n' '{escaped_entry}' >> '{known_hosts_path}'")

    # Set proper permissions on known_hosts file
    script_lines.append(f"chmod 600 '{known_hosts_path}'")

    return " && ".join(script_lines)


@pure
def build_add_authorized_keys_command(
    user: str,
    authorized_keys_entries: tuple[str, ...],
) -> str | None:
    """Build a shell command that adds entries to the user's authorized_keys file.

    This command:
    1. Creates the user's .ssh directory if it doesn't exist
    2. Appends each authorized_keys entry to the authorized_keys file

    Returns a shell command string that can be executed via sh -c, or None if
    there are no entries to add.
    """
    if not authorized_keys_entries:
        return None

    ssh_dir = get_user_ssh_dir(user)
    authorized_keys_path = ssh_dir / "authorized_keys"

    script_lines: list[str] = [
        # Create .ssh directory if needed
        f"mkdir -p '{ssh_dir}'",
    ]

    for entry in authorized_keys_entries:
        assert "'" not in entry, "Single quotes are not allowed in authorized_keys entries"
        # Append entry to authorized_keys (with a newline)
        script_lines.append(f"printf '%s\\n' '{entry}' >> '{authorized_keys_path}'")

    # Set proper permissions on authorized_keys file
    script_lines.append(f"chmod 600 '{authorized_keys_path}'")

    return " && ".join(script_lines)


@pure
def parse_warnings_from_output(output: str) -> list[str]:
    """Parse warning messages from command output.

    Looks for lines prefixed with WARNING_PREFIX and extracts the warning messages.

    Returns a list of warning messages (without the prefix).
    """
    warnings: list[str] = []
    for line in output.split("\n"):
        if line.startswith(WARNING_PREFIX):
            warning_message = line[len(WARNING_PREFIX) :].strip()
            if warning_message:
                warnings.append(warning_message)
    return warnings


def load_resource_script(filename: str) -> str:
    """Load a shell script from the mng resources package."""
    resource_files = importlib.resources.files(resources)
    script_path = resource_files.joinpath(filename)
    return script_path.read_text()


@pure
def build_start_volume_sync_command(
    volume_mount_path: str,
    mng_host_dir: str,
) -> str:
    """Build a shell command that starts a background loop to sync the host volume.

    The sync loop runs every 60 seconds and calls 'sync' on the volume mount
    path to flush any pending writes. This ensures data is persisted to the
    volume even if the sandbox is terminated without a clean shutdown.

    Returns a shell command string that can be executed via sh -c.
    """
    script_path = f"{mng_host_dir}/commands/volume_sync.sh"
    log_path = f"{mng_host_dir}/logs/volume_sync.log"

    # The sync script content (simple loop)
    sync_script = f"#!/bin/sh\nwhile true; do sync {volume_mount_path} 2>/dev/null; sleep 60; done\n"
    escaped_script = sync_script.replace("'", "'\"'\"'")

    script_lines = [
        f"mkdir -p '{mng_host_dir}/commands'",
        f"mkdir -p '{mng_host_dir}/logs'",
        f"printf '%s' '{escaped_script}' > '{script_path}'",
        f"chmod +x '{script_path}'",
        f"nohup '{script_path}' > '{log_path}' 2>&1 &",
    ]

    return " && ".join(script_lines)


@pure
def build_start_activity_watcher_command(
    mng_host_dir: str,
) -> str:
    """Build a shell command that installs and starts the activity watcher.

    The activity watcher monitors activity files and calls the shutdown script
    when the host becomes idle (based on idle_mode and idle_timeout_seconds
    from data.json).

    This command:
    1. Creates the commands directory
    2. Writes the shared logging library (mng_log.sh) to the host
    3. Writes the activity watcher script to the host
    4. Makes both executable
    5. Starts the activity watcher in the background with nohup

    Returns a shell command string that can be executed via sh -c.
    """
    log_lib_content = load_resource_script("mng_log.sh")
    script_content = load_resource_script("activity_watcher.sh")

    # Escape single quotes in script content
    escaped_log_lib = log_lib_content.replace("'", "'\"'\"'")
    escaped_script = script_content.replace("'", "'\"'\"'")

    log_lib_path = f"{mng_host_dir}/commands/mng_log.sh"
    script_path = f"{mng_host_dir}/commands/activity_watcher.sh"
    log_path = f"{mng_host_dir}/logs/activity_watcher.log"

    script_lines = [
        # Create commands and logs directories
        f"mkdir -p '{mng_host_dir}/commands'",
        f"mkdir -p '{mng_host_dir}/logs'",
        # Write the shared logging library
        f"printf '%s' '{escaped_log_lib}' > '{log_lib_path}'",
        f"chmod +x '{log_lib_path}'",
        # Write the activity watcher script
        f"printf '%s' '{escaped_script}' > '{script_path}'",
        # Make it executable
        f"chmod +x '{script_path}'",
        # Start the activity watcher in the background, redirecting output to log
        f"nohup '{script_path}' '{mng_host_dir}' > '{log_path}' 2>&1 &",
    ]

    return " && ".join(script_lines)
