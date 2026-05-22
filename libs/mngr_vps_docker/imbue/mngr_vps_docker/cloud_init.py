def generate_cloud_init_user_data(
    host_private_key: str,
    host_public_key: str,
    auto_shutdown_minutes: int | None = None,
) -> str:
    """Generate a cloud-init user_data script for VPS provisioning.

    Injects the SSH host key so we know it before the VPS boots (no TOFU),
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
package_update: true
packages:
  - ca-certificates
  - curl
  - rsync
  - docker.io
write_files:
  - path: /etc/ssh/sshd_config.d/99-mngr.conf
    permissions: '0644'
    content: |
      MaxSessions 100
      MaxStartups 100:30:200
runcmd:
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
