Part of the account-owned remote providers program (`specs/account-owned-providers/spec.md`); the entry `mngr-per-client-host-keys.md` in this directory belongs to the same PR.

- The `modal deploy` subprocess environment carries both main's extra env and the instance's own Modal tokens.

- Review fixes on top of the merge: `deploy()` carries its `extra_env` into the `modal deploy` subprocess again (the route deployer's `MNGR_MODAL_SOURCE_MARKER_NAME` had been dropped by the merge), and `is_function_deployed` authenticates with the instance's own client like every other SDK call.
