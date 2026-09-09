Improved the "Provider 'X' references unknown backend 'X'" configuration error when the backend's plugin is not installed at all.

Previously the message unconditionally advised adding `plugin = "<plugin-name>"` to the provider block whenever any plugin was disabled. That advice cannot resolve a backend whose plugin is not installed, so users who followed it hit the same error again, and the "Currently disabled plugins" list (which did not include the missing backend) added to the confusion.

The message now distinguishes the two cases: when the backend maps to an installed-but-disabled plugin, it still suggests enabling it or opting out with `plugin =`; when the backend is not registered at all, it leads with the concrete install hint (e.g. naming `imbue-mngr-azure`) and only mentions the `plugin =` path as a secondary "if instead" option when other plugins happen to be disabled.
