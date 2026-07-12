A plugin that fails to import (e.g. a provider whose dependency published a breaking release) no longer bricks the whole `mngr` CLI for the two commands you need to recover.

`mngr plugin remove` and `mngr plugin disable` now run without importing third-party plugin entry points, so a broken plugin can no longer crash the very command used to remove or disable it. To keep their behavior predictable, these two commands always skip the entry-point load (not only when something is broken), and they parse config leniently so a `[providers.<x>]` block belonging to the not-loaded plugin does not turn recovery into a new error.

Every other command is unchanged: it still loads all plugins and fails loudly if one is broken, rather than running in a degraded state.
