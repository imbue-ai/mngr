Fixed: a workspace's Permissions tab, its credential connects/disconnects, and its permission approvals no longer fail wholesale because your mngr settings configure a provider whose plugin this app does not ship.

The desktop app reads the same settings files the `mngr` CLI does, including the project settings of whatever checkout it was launched from, but it carries only the provider plugins it depends on. A `[providers.<name>]` block naming a backend it has no plugin for (azure and gcp are both configured in the mngr monorepo's own `.mngr/settings.toml`) was a parse error that took the whole settings load down with it, so every remote workspace reported "Could not load the mngr settings that name your workspaces' machines: Provider 'azure' references unknown backend 'azure'" and had no reachable machine.

Such a block is now skipped with a warning, and the workspaces whose providers this app does have keep working.
