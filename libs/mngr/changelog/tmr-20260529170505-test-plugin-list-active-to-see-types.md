Strengthened the `mngr plugin list --active` e2e tutorial test: it now verifies the
command actually surfaces the built-in agent types and that every listed plugin is
enabled. Added a companion test that disables a plugin and confirms `--active` filters
it out while the unfiltered list still reports it as disabled.
