def generate_cloud_init_user_data(
    host_private_key: str,
    host_public_key: str,
    auto_shutdown_minutes: int | None = None,
) -> str:
    """Generate a cloud-init user_data script for VPS provisioning.

    Injects the SSH host key so we know it before the VPS boots (no TOFU),
    forwards the provider's SSH key from the cloud-image default user
    (admin / ec2-user / ubuntu / etc.) into root's authorized_keys so
    mngr can SSH in as root on AMIs that don't put the key there directly,
    disables password authentication, installs Docker via the Debian
    ``docker.io`` package (handled inline by cloud-init's package handler
    -- about 5-15s on a ``t3.small``, vs 60-120s for the official
    ``curl get.docker.com | sh`` installer script which downloads the full
    docker-ce stack and configures Docker's own apt repo), and bumps sshd's
    ``MaxStartups`` / ``MaxSessions`` so the provisioning round-trips
    (image build + per-host setup + the imbue_cloud pool baking's many
    concurrent ``mngr exec`` / ``rsync`` / ``ssh`` calls) don't trip the
    default 10:30:100 pre-auth cap and lose connections mid-transfer.
    Mirrors the equivalent ``MaxSessions=100`` / ``MaxStartups=100:30:200``
    knob the lima provider applies to its VMs. The bump lives in a
    ``/etc/ssh/sshd_config.d/`` drop-in written via ``write_files``; sshd
    is already running with the cloud image's default config by the time
    cloud-init reaches this stage, so ``systemctl reload ssh`` (SIGHUP)
    in ``runcmd`` is what makes sshd re-read its config and pick up the
    drop-in. Reload is chosen over restart because SIGHUP preserves
    in-flight SSH sessions, which keeps the provisioning poll loop's
    connections alive across the bootstrap window.

    When ``auto_shutdown_minutes`` is set, the VPS schedules a
    ``shutdown -P +N`` from cloud-init, so the OS halts itself after the
    deadline. On AWS, paired with ``InstanceInitiatedShutdownBehavior=
    terminate``, this means the EC2 instance auto-terminates and stops
    billing even if the orchestrating process is killed. On Vultr the OS
    halts but billing continues until the VPS is destroyed -- still useful
    as a circuit-breaker so an abandoned VPS becomes obviously unreachable
    rather than silently consuming the agent slot.

    ``rsync`` is explicit in the package list because
    ``mngr_vps_docker._upload_directory_to_outer`` requires it for the
    build-context push. Standard Debian/Ubuntu cloud images ship rsync
    by default so this is belt-and-suspenders on cloud-init backends;
    non-cloud-init backends (e.g. OVH) install it from their own
    bootstrap path.

    ``curl`` is explicit because the depot CLI installer used at docker
    build time when ``builder=DEPOT`` shells out to
    ``curl -fsSL https://depot.dev/install-cli.sh | sh`` (see
    ``_DEPOT_INSTALL_CMD`` in ``instance.py``), and Debian cloud images
    ship ``wget`` but not ``curl`` by default.

    ``inotify-tools`` and ``jq`` are needed by the per-host
    ``snapshot_helper.sh`` (installed later, after the btrfs mount is
    ready) -- pre-baked here so the helper install via SSH only needs
    to drop files in place, no extra package install round-trips.
    """
    shutdown_block = ""
    if auto_shutdown_minutes is not None:
        shutdown_block = (
            f"  - shutdown -P +{auto_shutdown_minutes} "
            f"'mngr_vps_docker auto-shutdown after {auto_shutdown_minutes} minutes'\n"
        )
    return f"""#cloud-config
ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
{_indent(host_private_key, 4)}
  ed25519_public: {host_public_key}
ssh_pwauth: false
# Cloud-init disables root SSH by default (``disable_root: true``), which
# prefixes root's authorized_keys with a ``no-port-forwarding,no-X11-forwarding,
# no-agent-forwarding,no-pty,command="echo 'Please login as the user...'"``
# wrapper. mngr_vps_docker SSHes in as root and runs interactive shell-y
# commands via pyinfra, so that wrapper would silently break every poll.
# Set to false so root's authorized_keys takes the keys verbatim.
disable_root: false
package_update: true
packages:
  - ca-certificates
  - curl
  - rsync
  - docker.io
  - inotify-tools
  - jq
write_files:
  - path: /etc/ssh/sshd_config.d/99-mngr.conf
    permissions: '0644'
    content: |
      MaxSessions 100
      MaxStartups 100:30:200
runcmd:
  # Some cloud images install the provider-side SSH key into the default
  # user's authorized_keys (e.g. AWS Debian AMIs use 'admin', AL2/AL2023
  # use 'ec2-user', Ubuntu uses 'ubuntu') rather than root's. mngr_vps_docker
  # SSHes in as root (see ``_make_outer_for_vps_ip``), so without this
  # copy the provisioning poll loop would hang trying to authenticate.
  # Vultr / OVH put the key on root directly so this is a no-op there.
  # Paired with ``disable_root: false`` above so cloud-init doesn't prefix
  # root's keys with a ``no-pty,command="echo 'Please login as ...'"``
  # wrapper that would silently break every poll command.
  - mkdir -p /root/.ssh && chmod 0700 /root/.ssh
  - for u in admin ec2-user ubuntu debian fedora centos; do if [ -f "/home/$u/.ssh/authorized_keys" ]; then cat "/home/$u/.ssh/authorized_keys" >> /root/.ssh/authorized_keys; fi; done
  - touch /root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys
  - systemctl enable docker
  - systemctl start docker
  # Apply the MaxSessions/MaxStartups bump without killing in-flight SSH
  # connections. ``systemctl reload`` sends SIGHUP, which makes sshd
  # re-read its config (picking up the drop-in under
  # /etc/ssh/sshd_config.d/) while leaving existing sessions alive.
  # ``systemctl restart`` would tear those sessions down and race the
  # provisioning poll loop, hanging in-flight reads until pyinfra's
  # 10s per-command timeout fires.
  - systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || service ssh reload 2>/dev/null || true
  - touch /var/run/mngr-ready
{shutdown_block}"""


def _indent(text: str, spaces: int) -> str:
    """Indent each line of text by the given number of spaces."""
    prefix = " " * spaces
    return "\n".join(prefix + line for line in text.splitlines())
