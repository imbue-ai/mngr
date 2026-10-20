Fixed create attempts crashing when a second create was started before the first finished.

Every create attempt cloned its source repo into a single shared temp directory keyed on the repo name (`<tmp>/minds-clone-<repo_name>`), and deleted whatever was already there on the way in. That directory is the working directory of the attempt's `mngr create` subprocess, and the build context in every `[create_templates.*]` block is the relative path `"."`, which resolves against it. So starting a second create from the same template deleted the directory an in-flight create was standing in, and the older attempt died minutes later with an unreadable `FileNotFoundError` raised from `posixpath`.

The collision did not depend on the two creates sharing a launch mode: the path was keyed on the repo name alone, so an AWS create and a local Docker create from the same template collided just as hard. AWS-family creates were the most visible victims because they spend minutes provisioning before they touch the build context, but every launch mode was affected.

Each create attempt now gets its own private temp directory, removed when the attempt ends.

Added a startup sweep that reclaims scratch clones left behind by a previous session. Per-attempt cleanup runs in the attempt's `finally`, which a force-quit or crash skips because the create worker is a daemon thread, and a full default-workspace-template clone is around 240MB. Only directories untouched for a day are removed, so a clone belonging to a concurrently running second Minds instance is never deleted out from under it.
