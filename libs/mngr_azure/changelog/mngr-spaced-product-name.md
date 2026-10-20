Fixes the Azure provider failing to load at all. `azure-mgmt-resource` 26.0.0 removed the re-export at the top of `azure.mgmt.resource`, leaving it a namespace package whose only member is `resources`, so `from azure.mgmt.resource import ResourceManagementClient` raised `ImportError: cannot import name 'ResourceManagementClient' ... (unknown location)`.

The dependency is pinned `>=25` with no upper bound, so any fresh resolve picks 26 and the plugin stops importing. Because mngr loads every installed plugin through its entry points, one plugin that cannot import takes down whatever is loading them -- in the desktop app that meant `mngr latchkey forward` dying on startup and its per-agent event streams respawning in a loop.

The client is now imported from `azure.mgmt.resource.resources`, which is where it has always lived and which the next line of the same file was already importing `ResourceGroup` from.
