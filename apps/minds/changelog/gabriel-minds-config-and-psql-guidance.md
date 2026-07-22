Two startup / deploy failures now explain how to fix themselves instead of surfacing a cryptic error.

When `minds run` (including the desktop app, which passes the activated env's config path) is pointed at a client config file that does not exist, it no longer dies with a raw `Invalid value for '--config-file': File ... does not exist`. It now reports that the file is missing and, for a dev env, that its `client.toml` is only written by a successful deploy -- with the exact `minds env activate --deploy <name>` + `minds env deploy` commands to run. This is the common first-run wedge: activating a dev env with `--create` but never deploying it.

When `minds env deploy` cannot find the `psql` binary, its guidance now covers the macOS case correctly. Homebrew's `libpq` is keg-only, so `brew install libpq` alone leaves `psql` off PATH; the message now says to add it explicitly (`export PATH="$(brew --prefix libpq)/bin:$PATH"`) rather than implying the install is sufficient.
