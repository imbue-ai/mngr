Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entry `mngr-per-client-host-keys.md` in this directory belongs to the same PR.

- The OVH release trip runs its `mngr` commands from a temporary git repository (the `temp_git_repo` fixture), as the other cloud trips do, so the create no longer fails "Not inside a git repository" under the test isolation that moves the process cwd.

- The rebuild-time TOFU pin reads the VPS-root key path from the `VpsRootKeypair` that `mngr_vps` now resolves (no behavior change).
