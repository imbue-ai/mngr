## Internal: `_provision_vps` signature follows the shared base

- `OvhProvider._provision_vps` now accepts the `vps_public_key` parameter that the shared `VpsDockerProvider.create_host` threads in (so it no longer re-reads the provider SSH keypair from disk inside the base implementation). OVH installs the SSH public key via its rebuild API rather than the base cloud-init path, so the parameter is accepted and ignored. No behavioral change for OVH.

- The OVH release tests write a `settings.toml` that disables every other remote provider so the create-host preflight does not trip resolving their credentials. With the new `gcp` provider now registered as a remote backend, it is added to that disable-set. No behavioral change for OVH.
