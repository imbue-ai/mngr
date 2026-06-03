Added shell tab-completion for the positional arguments of `mngr plugin add` and `mngr plugin remove`.

- `mngr plugin add <TAB>` suggests installable plugin package names (e.g. `imbue-mngr-claude`, `imbue-mngr-modal`) drawn from the plugin catalog -- the same set the `mngr extras` install wizard offers.
- `mngr plugin remove <TAB>` suggests the plugin packages currently installed (from the uv-tool receipt), filtered to packages that actually register `mngr` entry points -- so non-plugin dependencies installed alongside plugins (e.g. workspace libraries) are not offered.

Both support prefix filtering and repeat the completion for each package when operating on several at once.
