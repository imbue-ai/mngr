# Add Lima VM Launch Mode to Minds App

## Overview

* The minds app currently supports two launch modes for creating agents: `LOCAL` (Docker container) and `DEV` (in-place on the local machine). A third mode is needed to run agents in Lima VMs.
* The Lima provider (`libs/mngr_lima/`) is already fully implemented with VM lifecycle management, SSH access, and persistent state. This change wires it into the minds app as a new `LIMA` launch mode.
* Users who need to store VM data on a separate drive can set the `LIMA_HOME` env var (a Lima-native mechanism) -- no custom symlink logic is needed in mngr.
* Disk space pre-creation checks are deferred to a future PR. Each VM gets its own separate base image (no sharing between instances).
* A new `[create_templates.lima]` is added to the forever-claude-template with small defaults (`--disk=5GiB`) suitable for testing on space-constrained machines.

## Expected Behavior

* The web UI create form (`/create`) shows a new "lima" option in the launch mode dropdown alongside "local" and "dev"
* Selecting "lima" and submitting the form creates an agent inside a Lima VM, using the address format `agent_name@agent_name-host.lima`
* The API endpoint `POST /api/create-agent` accepts `"launch_mode": "LIMA"` in the JSON body
* The resulting `mngr create` command uses `--template main --new-host --template lima`, analogous to how Docker uses `--template main --new-host --template docker`
* The lima template in the forever-claude-template configures: `provider = "lima"`, `target_path = "/code/"`, `idle_timeout = "2147483647"`, and default start args `--cpus=2 --memory=4 --disk=5`
* The `CLOUD` launch mode remains unimplemented (raises `NotImplementedError`)

## Changes

* `apps/minds/imbue/minds/primitives.py`: Add `LIMA = auto()` to the `LaunchMode` enum
* `apps/minds/imbue/minds/desktop_client/agent_creator.py`: Add `LaunchMode.LIMA` case to `_build_mngr_create_command()` -- address is `agent_name@agent_name-host.lima`, appends `--new-host --template lima`
* `apps/minds/imbue/minds/desktop_client/templates.py`: Update the help text for the launch mode selector to mention Lima (the dropdown itself auto-populates from the enum)
* `~/project/forever-claude-template/.mngr/settings.toml`: Add `[create_templates.lima]` with `provider = "lima"`, `target_path = "/code/"`, `idle_timeout = "2147483647"`, and `start_arg = ["--cpus=2", "--memory=4GiB", "--disk=5GiB"]`
* Existing tests in the minds app that exercise agent creation should be extended to cover the new `LIMA` launch mode (at minimum, test that `_build_mngr_create_command` produces the correct command)

### Resulting command for manual testing

When a user creates a mind named `my-agent` with lima mode from the forever-claude-template, the resulting command will be:

```
mngr create my-agent@my-agent-host.lima \
    --id <agent_id> \
    --no-connect \
    --reuse \
    --update \
    --label mind=my-agent \
    --template main \
    --new-host \
    --template lima
```

Run from the cloned template directory, which contains `.mngr/settings.toml` with the lima template that sets `provider = "lima"`, `target_path = "/code/"`, `idle_timeout = "2147483647"`, and `start_arg = ["--cpus=2", "--memory=4GiB", "--disk=5GiB"]`.
