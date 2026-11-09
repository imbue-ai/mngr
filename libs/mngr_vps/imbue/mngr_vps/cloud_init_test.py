"""Tests for cloud-init user_data generation."""

import yaml
from inline_snapshot import snapshot

from imbue.mngr_vps.cloud_init import _indent
from imbue.mngr_vps.cloud_init import generate_cloud_init_user_data
from imbue.mngr_vps.host_setup import PINNED_DOCKER_VERSION
from imbue.mngr_vps.host_setup import PINNED_GVISOR_RELEASE

# A realistic multi-line private key so the snapshot/YAML-parse tests exercise
# ``_indent`` and any future regression in the (load-bearing) indentation of the
# embedded key flips the snapshot below.
_SAMPLE_PRIVATE_KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nline-one\nline-two\n-----END OPENSSH PRIVATE KEY-----"
_SAMPLE_PUBLIC_KEY = "ssh-ed25519 AAAATESTKEY comment"


def test_indent_single_line() -> None:
    result = _indent("hello", 4)
    assert result == "    hello"


def test_indent_multiple_lines() -> None:
    result = _indent("line1\nline2\nline3", 2)
    assert result == "  line1\n  line2\n  line3"


def test_indent_zero_spaces() -> None:
    result = _indent("hello", 0)
    assert result == "hello"


def test_indent_empty_string() -> None:
    result = _indent("", 4)
    # Empty string has no lines, so splitlines returns [] and join returns ""
    assert result == ""


def test_generate_cloud_init_full_user_data_snapshot() -> None:
    # Full-document snapshot: cloud-init user_data is structured YAML where
    # indentation and key placement are load-bearing, so a substring check
    # cannot tell "the string is present" from "the document is correct". The
    # snapshot pins the exact rendered output; any structural regression (wrong
    # nesting of the private key, reordered/duplicated keys, a flipped flag)
    # changes it. Update intentionally via ``--inline-snapshot=fix``.
    result = generate_cloud_init_user_data(
        host_private_key=_SAMPLE_PRIVATE_KEY,
        host_public_key=_SAMPLE_PUBLIC_KEY,
        install_gvisor_runtime=False,
    )
    assert result == snapshot("""\
#cloud-config
ssh_deletekeys: true
ssh_keys:
  ed25519_private: |
    -----BEGIN OPENSSH PRIVATE KEY-----
    line-one
    line-two
    -----END OPENSSH PRIVATE KEY-----
  ed25519_public: ssh-ed25519 AAAATESTKEY comment
ssh_pwauth: false
# Cloud-init disables root SSH by default (``disable_root: true``), which
# prefixes root's authorized_keys with a ``no-port-forwarding,no-X11-forwarding,
# no-agent-forwarding,no-pty,command="echo 'Please login as the user...'"``
# wrapper. mngr_vps SSHes in as root and runs interactive shell-y
# commands via pyinfra, so that wrapper would silently break every poll.
# Set to false so root's authorized_keys takes the keys verbatim.
disable_root: false
runcmd:
  # Some cloud images install the provider-side SSH key into the default
  # user's authorized_keys (e.g. AWS Debian AMIs use 'admin', AL2/AL2023
  # use 'ec2-user', Ubuntu uses 'ubuntu') rather than root's. mngr_vps
  # SSHes in as root (see ``_make_outer_for_vps_ip``), so without this
  # copy the provisioning poll loop would hang trying to authenticate.
  # Vultr / OVH put the key on root directly so this is a no-op there.
  # Paired with ``disable_root: false`` above so cloud-init doesn't prefix
  # root's keys with a ``no-pty,command="echo 'Please login as ...'"``
  # wrapper that would silently break every poll command. Runs before the
  # shared host-setup steps so root SSH becomes reachable while the long
  # apt/Docker installs are still in flight.
  - mkdir -p /root/.ssh && chmod 0700 /root/.ssh
  - for u in admin ec2-user ubuntu debian fedora centos; do if [ -f "/home/$u/.ssh/authorized_keys" ]; then cat "/home/$u/.ssh/authorized_keys" >> /root/.ssh/authorized_keys; fi; done
  - touch /root/.ssh/authorized_keys && chmod 0600 /root/.ssh/authorized_keys
  - |
      set -e
      install -d -m 1777 /var/lib/mngr-tmp
      MNGR_TMP_STAGE="$(mktemp -d /var/lib/mngr-tmp/.mngr-stage.XXXXXX)"
      cat > "$MNGR_TMP_STAGE/unit" <<'MNGR_TMP_MOUNT_UNIT_EOF'
      [Unit]
      Description=mngr: /tmp on the VM's disk instead of a RAM-backed tmpfs
      DefaultDependencies=no
      Conflicts=umount.target
      Before=local-fs.target umount.target
      After=local-fs-pre.target
      ConditionPathIsDirectory=/var/lib/mngr-tmp
      [Mount]
      What=/var/lib/mngr-tmp
      Where=/tmp
      Type=none
      Options=bind,nosuid,nodev
      [Install]
      WantedBy=local-fs.target
      MNGR_TMP_MOUNT_UNIT_EOF
      if ! cmp -s "$MNGR_TMP_STAGE/unit" /etc/systemd/system/tmp.mount; then
          install -m 0644 "$MNGR_TMP_STAGE/unit" /etc/systemd/system/tmp.mount
          systemctl daemon-reload
          systemctl enable tmp.mount
      fi
      rm -rf "$MNGR_TMP_STAGE"
      if ! findmnt -no SOURCE /tmp 2>/dev/null | grep -q '/var/lib/mngr-tmp\\]$'; then
          systemctl restart tmp.mount || { umount -l /tmp && systemctl restart tmp.mount; }
      fi
      findmnt -no SOURCE /tmp | grep -q '/var/lib/mngr-tmp\\]$'
  - |
      set -e
      export DEBIAN_FRONTEND=noninteractive
      for attempt in 1 2 3 4 5 6; do
          if apt-get update; then break; fi
          if [ "$attempt" = 6 ]; then echo "apt-get update failed after 6 attempts" >&2; exit 1; fi
          sleep 10
      done
      apt-get install -y curl ca-certificates gnupg rsync inotify-tools jq
  - |
      set -e
      MNGR_BOUNDS_STAGE="$(mktemp -d)"
      mkdir -p /etc/docker
      if [ -f /etc/docker/daemon.json ]; then
          jq -S . /etc/docker/daemon.json > "$MNGR_BOUNDS_STAGE/current.json"
      else
          echo '{}' > "$MNGR_BOUNDS_STAGE/current.json"
      fi
      jq -S --argjson bounds '{"builder": {"gc": {"defaultKeepStorage": "1GB", "enabled": true}}, "log-driver": "json-file", "log-opts": {"max-file": "3", "max-size": "50m"}}' '. * $bounds' "$MNGR_BOUNDS_STAGE/current.json" > "$MNGR_BOUNDS_STAGE/merged.json"
      if ! cmp -s "$MNGR_BOUNDS_STAGE/current.json" "$MNGR_BOUNDS_STAGE/merged.json"; then
          install -m 0644 "$MNGR_BOUNDS_STAGE/merged.json" /etc/docker/daemon.json
          if systemctl is-active --quiet docker; then systemctl restart docker; fi
      fi
      rm -rf "$MNGR_BOUNDS_STAGE"
      mkdir -p /etc/systemd/journald.conf.d
      MNGR_JOURNALD_STAGE="$(mktemp)"
      printf '[Journal]\\nSystemMaxUse=512M\\n' > "$MNGR_JOURNALD_STAGE"
      if ! cmp -s "$MNGR_JOURNALD_STAGE" /etc/systemd/journald.conf.d/60-mngr.conf; then
          install -m 0644 "$MNGR_JOURNALD_STAGE" /etc/systemd/journald.conf.d/60-mngr.conf
          systemctl restart systemd-journald
      fi
      rm -f "$MNGR_JOURNALD_STAGE"
  - |
      set -e
      export DEBIAN_FRONTEND=noninteractive
      . /etc/os-release
      DOCKER_APT_VERSION="5:29.6.2-1~${ID}.${VERSION_ID}~${VERSION_CODENAME}"
      CONTAINERD_APT_VERSION="2.2.6-1~${ID}.${VERSION_ID}~${VERSION_CODENAME}"
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/${ID}/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" > /etc/apt/sources.list.d/docker.list
      apt-get update
      apt-get install -y --allow-downgrades docker-ce="${DOCKER_APT_VERSION}" docker-ce-cli="${DOCKER_APT_VERSION}" containerd.io="${CONTAINERD_APT_VERSION}" docker-buildx-plugin docker-compose-plugin
      systemctl enable docker
      systemctl start docker
  - |
      set -e
      MNGR_SSHD_STAGE="$(mktemp -d)"
      printf 'MaxSessions 100\\nMaxStartups 100:30:200\\nPermitRootLogin prohibit-password\\nPasswordAuthentication no\\nKbdInteractiveAuthentication no\\n' > "$MNGR_SSHD_STAGE/60-mngr.conf"
      if sshd -T 2>/dev/null | grep -qi '^persourcepenalties'; then printf 'PerSourcePenalties no\\n' >> "$MNGR_SSHD_STAGE/60-mngr.conf"; fi
      mkdir -p /etc/ssh/sshd_config.d
      if ! cmp -s "$MNGR_SSHD_STAGE/60-mngr.conf" /etc/ssh/sshd_config.d/60-mngr.conf; then
          install -m 0644 "$MNGR_SSHD_STAGE/60-mngr.conf" /etc/ssh/sshd_config.d/60-mngr.conf
          systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart 2>/dev/null || true
      fi
      rm -rf "$MNGR_SSHD_STAGE"
  - |
      set -e
      MNGR_MEMORY_STAGE="$(mktemp -d)"
      cat > "$MNGR_MEMORY_STAGE/script" <<'MNGR_MEMORY_SCRIPT_EOF'
      #!/bin/sh
      # Installed by mngr_vps: cap every VM-sized mngr agent container at the VM's RAM minus a reserve.
      set -u
      attempt=0
      until docker info >/dev/null 2>&1; do
          attempt=$((attempt + 1))
          if [ "$attempt" -ge 30 ]; then
              echo "WARNING: docker did not become available; leaving container memory caps unchanged" >&2
              exit 0
          fi
          sleep 2
      done
      total_kib=$(awk '/^MemTotal:/{print $2}' /proc/meminfo)
      cap_mib=$(( total_kib / 1024 - 1024 ))
      if [ "$cap_mib" -le 0 ]; then exit 0; fi
      cap_bytes=$(( cap_mib * 1024 * 1024 ))
      for container_id in $(docker ps -aq --filter "label=com.imbue.mngr.host-id" --filter "label=com.imbue.mngr.memory-cap=vm"); do
          current=$(docker inspect --format '{{.HostConfig.Memory}}' "$container_id")
          if [ "$current" != "$cap_bytes" ]; then
              docker update --memory "${cap_mib}m" --memory-swap "${cap_mib}m" "$container_id" \\
                  || echo "WARNING: could not update the memory cap of $container_id" >&2
          fi
      done
      MNGR_MEMORY_SCRIPT_EOF
      cat > "$MNGR_MEMORY_STAGE/unit" <<'MNGR_MEMORY_UNIT_EOF'
      [Unit]
      Description=mngr: cap the agent container's memory at the VM's RAM minus a reserve
      After=docker.service
      Wants=docker.service
      [Service]
      Type=oneshot
      ExecStart=/usr/local/sbin/mngr-vps-container-memory.sh
      [Install]
      WantedBy=multi-user.target
      MNGR_MEMORY_UNIT_EOF
      if ! cmp -s "$MNGR_MEMORY_STAGE/script" /usr/local/sbin/mngr-vps-container-memory.sh || ! cmp -s "$MNGR_MEMORY_STAGE/unit" /etc/systemd/system/mngr-vps-container-memory.service; then
          install -m 0755 "$MNGR_MEMORY_STAGE/script" /usr/local/sbin/mngr-vps-container-memory.sh
          install -m 0644 "$MNGR_MEMORY_STAGE/unit" /etc/systemd/system/mngr-vps-container-memory.service
          systemctl daemon-reload
          systemctl enable mngr-vps-container-memory.service
      fi
      rm -rf "$MNGR_MEMORY_STAGE"
  - |
      set -e
      MNGR_MEMINFO_STAGE="$(mktemp -d)"
      cat > "$MNGR_MEMINFO_STAGE/script" <<'MNGR_MEMINFO_SCRIPT_EOF'
      #!/usr/bin/perl
      # Installed by mngr_vps: publish each agent container's memory headroom into its volume.
      use strict;
      use warnings;
      use Fcntl qw(O_WRONLY O_CREAT O_EXCL O_NOFOLLOW O_NONBLOCK);
      \n\
      my $cgroup_parent = '/sys/fs/cgroup/system.slice';
      my $host_id_label = 'com.imbue.mngr.host-id';
      my $volume_mount_path = '/mngr-vol';
      my $file_name = '.host-meminfo';
      my $interval_seconds = 0.25;
      my $rounds_per_discovery = 40;
      \n\
      # docker's output, or undef if it fails. Its stderr is dropped: a container
      # that stops mid-discovery fails its inspect.
      sub docker_output {
          my $pid = open(my $out, '-|') // return undef;
          if ($pid == 0) {
              open(STDERR, '>', '/dev/null');
              exec('docker', @_) or exit 127;
          }
          my $output = do { local $/; <$out> } // '';
          close($out) or return undef;
          chomp $output;
          return $output;
      }
      \n\
      # [cgroup directory, volume directory] for each running agent host container.
      # The volume directory is the named volume's bind device, the subvolume itself
      # rather than docker's own mount of it.
      sub discover {
          my @targets;
          my $ids = docker_output('ps', '-q', '--no-trunc', '--filter', "label=$host_id_label") // return ();
          for my $container_id (split ' ', $ids) {
              my $volume_name = docker_output(
                  'inspect', '--format',
                  '{{range .Mounts}}{{if eq .Destination "' . $volume_mount_path . '"}}{{.Name}}{{end}}{{end}}',
                  $container_id,
              );
              next unless defined $volume_name && $volume_name ne '';
              my $volume_dir = docker_output('volume', 'inspect', '--format', '{{.Options.device}}', $volume_name);
              next unless defined $volume_dir && $volume_dir ne '' && -d $volume_dir;
              push @targets, ["$cgroup_parent/docker-$container_id.scope", $volume_dir];
          }
          return @targets;
      }
      \n\
      sub read_first_line {
          my ($path) = @_;
          open(my $fh, '<', $path) or return undef;
          my $line = <$fh>;
          close($fh);
          return undef unless defined $line;
          chomp $line;
          return $line;
      }
      \n\
      # Replaces $path by rename from a fresh temp file beside it, created so that
      # no planted name can be written through. True once $path holds $content.
      sub write_replacing {
          my ($path, $content) = @_;
          my ($fh, $tmp);
          for (1 .. 100) {
              $tmp = sprintf('%s.%08x', $path, int(rand(4294967296)));
              last if sysopen($fh, $tmp, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0644);
              undef $fh;
          }
          return 0 unless defined $fh;
          print {$fh} $content;
          close($fh) or do { unlink $tmp; return 0 };
          return 1 if rename($tmp, $path);
          unlink $tmp;
          return 0;
      }
      \n\
      # Overwrites $path in place, so it keeps its inode. Only a regular file of
      # exactly the content's length is overwritten; anything else (the first write,
      # an older format, a planted symlink or fifo) is replaced by rename. True once
      # $path holds $content.
      sub write_in_place {
          my ($path, $content) = @_;
          if (sysopen(my $fh, $path, O_WRONLY | O_NOFOLLOW | O_NONBLOCK)) {
              if (-f $fh && -s _ == length($content)) {
                  my $written = syswrite($fh, $content);
                  close($fh);
                  return 1 if defined $written && $written == length($content);
              } else {
                  close($fh);
              }
          }
          return write_replacing($path, $content);
      }
      \n\
      # Points $path at a complete copy of $content while the sandbox only ever sees
      # two inodes. gVisor's gofer keeps an entry for every distinct inode the sandbox
      # sees, for its lifetime, so replacing $path with a fresh file on every rewrite
      # grows it without bound; overwriting $path itself would let a reader catch a
      # half-written file. Instead the content is written in place into whichever of
      # two slot files $path does not name, which is then renamed onto $path through
      # a hard link.
      sub publish_through_slot {
          my ($path, $content) = @_;
          my @published = lstat($path);
          my @slot0 = lstat("$path.slot0");
          my $slot = @published && @slot0 && $published[0] == $slot0[0] && $published[1] == $slot0[1] ? 1 : 0;
          my $slot_path = "$path.slot$slot";
          write_in_place($slot_path, $content) or return;
          my $link;
          for (1 .. 100) {
              $link = sprintf('%s.%08x', $path, int(rand(4294967296)));
              last if link($slot_path, $link);
              return unless $!{EEXIST};
              undef $link;
          }
          return unless defined $link;
          # A rename onto a name for the same file does nothing and leaves $link behind.
          rename($link, $path);
          unlink $link;
      }
      \n\
      sub withdraw {
          my ($target_file) = @_;
          unlink $target_file, "$target_file.slot0", "$target_file.slot1";
      }
      \n\
      sub publish_memory {
          my ($cgroup_dir, $target_file) = @_;
          # A container can stop, and its cgroup vanish, between any two reads.
          my $limit = read_first_line("$cgroup_dir/memory.max") // return;
          if ($limit eq 'max') {
              withdraw($target_file);
              return;
          }
          return unless $limit =~ /^\\d+$/;
          my $usage = read_first_line("$cgroup_dir/memory.current") // return;
          return unless $usage =~ /^\\d+$/;
          open(my $stat, '<', "$cgroup_dir/memory.stat") or return;
          my $inactive_file = 0;
          while (my $line = <$stat>) {
              my ($key, $value) = split ' ', $line;
              next unless defined $value && $value =~ /^\\d+$/;
              $inactive_file = $value if $key eq 'inactive_file';
          }
          close($stat);
          my $available = $limit - $usage + $inactive_file;
          $available = 0 if $available < 0;
          $available = $limit if $available > $limit;
          # Fixed-width fields, so every rewrite of a slot has the same length (see write_in_place).
          publish_through_slot(
              $target_file,
              sprintf(
                  "MemTotal: %15d kB\\nMemAvailable: %15d kB\\nTimestamp: %15d\\n",
                  $limit / 1024, $available / 1024, time,
              ),
          );
      }
      \n\
      my @targets;
      for (my $round = 0; ; $round++) {
          @targets = discover() if $round % $rounds_per_discovery == 0;
          for my $target (@targets) {
              my ($cgroup_dir, $volume_dir) = @$target;
              publish_memory($cgroup_dir, "$volume_dir/$file_name");
          }
          select(undef, undef, undef, $interval_seconds);
      }
      MNGR_MEMINFO_SCRIPT_EOF
      cat > "$MNGR_MEMINFO_STAGE/unit" <<'MNGR_MEMINFO_UNIT_EOF'
      [Unit]
      Description=mngr: publish the agent container's memory headroom into its volume
      After=docker.service
      Wants=docker.service
      [Service]
      Type=simple
      ExecStart=/usr/local/sbin/mngr-vps-container-meminfo.pl
      Restart=always
      RestartSec=2
      [Install]
      WantedBy=multi-user.target
      MNGR_MEMINFO_UNIT_EOF
      if ! cmp -s "$MNGR_MEMINFO_STAGE/script" /usr/local/sbin/mngr-vps-container-meminfo.pl || ! cmp -s "$MNGR_MEMINFO_STAGE/unit" /etc/systemd/system/mngr-vps-container-meminfo.service; then
          install -m 0755 "$MNGR_MEMINFO_STAGE/script" /usr/local/sbin/mngr-vps-container-meminfo.pl
          install -m 0644 "$MNGR_MEMINFO_STAGE/unit" /etc/systemd/system/mngr-vps-container-meminfo.service
          systemctl daemon-reload
          systemctl enable mngr-vps-container-meminfo.service
          systemctl restart mngr-vps-container-meminfo.service
      fi
      rm -rf "$MNGR_MEMINFO_STAGE"
  - touch /var/run/mngr-ready
""")


def test_generate_cloud_init_contains_host_key() -> None:
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntest-key-content\n-----END OPENSSH PRIVATE KEY-----"
    public_key = "ssh-ed25519 AAAA testkey"

    result = generate_cloud_init_user_data(
        host_private_key=private_key,
        host_public_key=public_key,
        install_gvisor_runtime=False,
    )

    assert "test-key-content" in result
    assert public_key in result


def test_generate_cloud_init_disables_password_auth() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "ssh_pwauth: false" in result


def test_generate_cloud_init_installs_pinned_docker() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    # Pinned install via the official Docker apt repo (not the unpinned get.docker.com script).
    # Repo + apt version are derived per-distro from /etc/os-release at run time.
    assert "download.docker.com/linux/${ID}" in result
    assert PINNED_DOCKER_VERSION in result
    assert 'docker-ce="${DOCKER_APT_VERSION}"' in result
    assert "--allow-downgrades" in result
    assert "systemctl enable docker" in result
    assert "systemctl start docker" in result
    # The slow installer-script approach must NOT come back -- it was the
    # root cause of the EC2 lifecycle test hitting the 300s subprocess
    # timeout on the ``mngr create`` flow.
    assert "get.docker.com" not in result


def test_generate_cloud_init_forwards_ssh_key_to_root() -> None:
    """Regression: AMIs whose cloud image installs the provider SSH key on the
    default user (admin / ec2-user / ubuntu / etc.) instead of root would make
    mngr's root-targeted SSH hang (we connect as root per ``ssh_user="root"``
    in ``mngr_vps.instance._make_outer_for_vps_ip``). cloud-init's
    runcmd copies the default user's authorized_keys into ``/root/.ssh``
    before mngr's provisioning poll loop runs, so root SSH always works.

    Vultr / OVH already install the key on root directly so this is a no-op
    there -- but emitting the shell on every provider is cheaper than
    branching in Python by provider.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "/root/.ssh/authorized_keys" in result
    assert "admin" in result
    assert "ec2-user" in result
    assert "ubuntu" in result


def test_generate_cloud_init_omits_direct_root_key_by_default() -> None:
    """Without ``authorized_user_public_key`` the direct-root-inject line is absent."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "printf '%s\\n'" not in result


def test_generate_cloud_init_injects_authorized_user_public_key_into_root() -> None:
    """With ``authorized_user_public_key`` set, the key is written straight into root.

    Removes the dependency on a cloud image's default-user copy landing in root
    -- notably on GCE, where the guest agent provisions the key asynchronously
    and races the runcmd copy. The key is shell-quoted so its embedded space /
    comment survive.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA host",
        install_gvisor_runtime=False,
        authorized_user_public_key="ssh-ed25519 AAAAaccess user@laptop",
    )
    assert "'ssh-ed25519 AAAAaccess user@laptop'" in result
    assert ">> /root/.ssh/authorized_keys" in result


def test_generate_cloud_init_disables_root_lockout() -> None:
    """Cloud-init defaults to ``disable_root: true``, which prefixes root's
    authorized_keys with a ``no-port-forwarding,no-X11-forwarding,no-agent-
    forwarding,no-pty,command="echo 'Please login as the user ...'"``
    wrapper. mngr_vps SSHes in as root and runs shell-y pyinfra
    commands via that account, so the wrapper would silently break every
    poll. The generated cloud-init must set ``disable_root: false`` so the
    forwarded key lands without the wrapper.
    """
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "disable_root: false" in result


def test_generate_cloud_init_creates_ready_marker() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "touch /var/run/mngr-ready" in result


def test_generate_cloud_init_deletes_existing_keys() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "ssh_deletekeys: true" in result


def test_generate_cloud_init_no_shutdown_by_default() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "shutdown -P" not in result


def test_generate_cloud_init_with_auto_shutdown_adds_shutdown_command() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_seconds=42 * 60,
    )
    assert "shutdown -P +42" in result


def test_generate_cloud_init_rounds_sub_minute_shutdown_up_to_whole_minutes() -> None:
    """`shutdown -P` takes whole minutes, so seconds round up (and never to 0)."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_seconds=90,
    )
    assert "shutdown -P +2" in result


def test_generate_cloud_init_with_auto_shutdown_appears_in_runcmd() -> None:
    """The shutdown entry must be inside the runcmd block, not loose YAML."""
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
        auto_shutdown_seconds=15 * 60,
    )
    runcmd_index = result.index("runcmd:")
    shutdown_index = result.index("shutdown -P +15")
    assert runcmd_index < shutdown_index
    # The shutdown line is a list item under runcmd (starts with "  - ").
    line = next(line for line in result.splitlines() if "shutdown -P" in line)
    assert line.lstrip().startswith("- shutdown -P")


def test_generate_cloud_init_omits_gvisor_install_by_default() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=False,
    )
    assert "runsc" not in result
    assert "gvisor" not in result


def test_generate_cloud_init_includes_gvisor_install_when_requested() -> None:
    result = generate_cloud_init_user_data(
        host_private_key="fake-key",
        host_public_key="ssh-ed25519 AAAA fake",
        install_gvisor_runtime=True,
    )
    # Downloads the pinned dated gVisor release and registers it with the daemon,
    # with --overlay2=none so the writable layer persists across container restart.
    assert f"gvisor/releases/release/{PINNED_GVISOR_RELEASE}" in result
    assert "runsc install -- --overlay2=none" in result
    # gnupg is installed with the base packages (needed for the Docker apt key).
    assert "gnupg" in result


def test_generate_cloud_init_parses_as_yaml_with_key_at_correct_nesting() -> None:
    # Beyond the textual snapshot, prove the output is well-formed YAML and that
    # the private key lands under ``ssh_keys.ed25519_private`` with its content
    # intact -- a wrong-indentation regression would either fail to parse here
    # or surface the key at the wrong nesting level, which a substring check
    # could never catch. (cloud-init user_data is YAML by definition; this test
    # parses the format we are forced to emit, it does not introduce new YAML.)
    result = generate_cloud_init_user_data(
        host_private_key=_SAMPLE_PRIVATE_KEY,
        host_public_key=_SAMPLE_PUBLIC_KEY,
        install_gvisor_runtime=False,
    )
    assert result.startswith("#cloud-config\n")

    parsed = yaml.safe_load(result)
    # The block scalar preserves the key content (with a trailing newline).
    assert parsed["ssh_keys"]["ed25519_private"].strip() == _SAMPLE_PRIVATE_KEY
    assert parsed["ssh_keys"]["ed25519_public"] == _SAMPLE_PUBLIC_KEY
    assert parsed["ssh_pwauth"] is False
    assert parsed["ssh_deletekeys"] is True
    # The provisioning commands are emitted as a list of runcmd shell scripts;
    # the Docker install and ready-marker steps must survive the YAML round-trip.
    runcmd_joined = "\n".join(parsed["runcmd"])
    assert "systemctl enable docker" in runcmd_joined
    assert "systemctl start docker" in runcmd_joined
    assert "touch /var/run/mngr-ready" in runcmd_joined
