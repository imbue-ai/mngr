## Plugin Execution and Ordering

During create, plugins must verify that they have enough auth/credential information [future: incomplete for Claude agent] in order for it to be worth making the host *before* we make the host

They should *not* verify that the information is correct--simply that it is present in the correct form.

(obviously agents can end up becoming unauthorized later, in which case plugins should generally be presenting errors in their state and whenever a command is run that uses the plugin's functionality)

### Plugin Stacking and Dependencies [future]

**Open Question**: How should plugins "stack" when one plugin depends on or extends another?

For example, if we have a "default-claude" plugin and a "better-claude" plugin, the latter might need to run after the former to override or extend its behavior. This creates a dependency relationship between plugins.

Potential approaches:
1. Explicit dependency declaration in plugin metadata (similar to Python package dependencies)
2. A priority/ordering system for plugin hooks
3. Allow plugins to query and depend on the presence of other plugins

This needs to be resolved to support composable plugin ecosystems where plugins can build on each other's functionality.

## Offline Access

If plugins restrict themselves to accessing just data via read_file for files within the `MNGR_HOST_DIR`, then the offline_mngr_state plugin [future] will allow offline access to that data.

Obviously commands will always work with local agents, since the data is local.

## Provisioning Tools

Plugins CAN use tools other than pyinfra for setting up their dependencies, but they should avoid doing so unless they have a really good reason. Using non-standard tools:
- Makes provisioning harder to understand and debug
- Can conflict with other plugins' provisioning logic
- Reduces the benefits of pyinfra's idempotent operations

**pyinfra should only be used for provisioning.** It should not be used for other operations like file syncing, command execution during normal operation, etc. Those operations should use direct SSH or other appropriate mechanisms.
