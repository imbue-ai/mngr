## host-setup: OS-aware Docker install

- The pinned Docker install step derives the apt repo (`download.docker.com/linux/$ID`) and the full apt version suffix (`~$ID.$VERSION_ID~$VERSION_CODENAME`) from `/etc/os-release` at run time rather than hardcoding the Debian 12 / bookworm strings, so it is distro-aware across the Debian family. On the Debian 12 default it renders the same apt version (`5:29.5.1-1~debian.12~bookworm`) and repo URL (`linux/debian`) every backend already used; the derivation additionally covers a non-default `--gcp-image` (e.g. an Ubuntu LTS image) without a code change. `PINNED_DOCKER_APT_VERSION` is exported as the fully-rendered Debian 12 apt version string for any caller or test that needs the exact value rather than the runtime-derived suffix.

## bootstrap: direct root-key injection

- The first-boot bootstrap (cloud-init `user-data` and the GCE `startup-script`) writes the provider SSH public key straight into root's `authorized_keys`, independent of the copy-from-default-user (`admin` / `ec2-user` / `ubuntu` / `debian` / ...) step, via an `authorized_user_public_key` parameter that `_provision_vps` always passes. This removes any reliance on a cloud image's default-user key landing in root. It is the deciding fix for GCE, where the google guest agent provisions the `ssh-keys` metadata into the `ubuntu` user asynchronously and races the default-user copy, intermittently leaving root without the key. Additive and idempotent for the AWS / Vultr / OVH backends (the key also lands in root via the default-user copy, so the extra line is a no-op duplicate).

## bootstrap: backend override hooks for non-cloud-init images

- `VpsDockerProvider` gains two override hooks: `_generate_bootstrap_payload` (default cloud-init `user-data`; a backend whose images run the google-guest-agent instead of cloud-init, e.g. GCP, overrides it to render a `startup-script`) and `_wait_for_expected_host_key` (default no-op; overridden when the host key is installed post-boot, to wait for it before strict-checking). Provisioning is otherwise backend-agnostic -- both payloads render the same shared `host_setup.build_host_setup_steps` and write the same marker.

- The `mngr-ready` first-boot completion marker path is now the single `host_setup.MNGR_READY_MARKER_PATH` constant, shared by both bootstrap renderers and the poller.

## create_host: pre-create validation runs before any provider write

- `VpsDockerProvider.create_host` now calls the `_validate_provider_args_for_create` hook before the first provider API write (the SSH key upload), instead of partway through `_provision_vps`. This means a provider-specific pre-create precondition that fails -- e.g. GCP's missing-firewall check -- aborts cleanly with no instance created, no SSH key uploaded, and no `Host creation failed, attempting cleanup...` path. The hook's docstring now reflects this contract (cheap, local or single read-only check, before any write). Behaviorally a no-op for providers whose hook is the default no-op or a cheap local guard (AWS's pytest auto-shutdown check).
