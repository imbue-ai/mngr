Fixed: Stop blaming package installation when Modal kills the sandbox during host bring-up (MIND-235).

- `mngr create`/`mngr start` on Modal now check that a newly created sandbox actually runs a command before provisioning it, and replace it (up to three tries) when it does not. Modal kills a small fraction of sandboxes on the way up, and every command queued against a dead sandbox comes back SIGKILLed (exit 137), so host creation used to fail outright on an infrastructure hiccup.

- A bring-up step that fails because the sandbox itself is gone now raises `ModalSandboxDiedMngrError` naming the sandbox and its exit code, instead of "Failed to install required packages (exit code 137)" -- a message that was misleading, since the default image already ships those packages and apt never ran.
