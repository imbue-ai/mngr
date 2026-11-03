On Linux the app's files sit on the XDG base directories, one per role, as macOS
already does with `~/Library`, instead of in a single `~/.minds` dotfolder:

- state (credentials, agent records, the virtualenv, the managed CPython,
  Electron's `userData`) at `$XDG_DATA_HOME/imbue-studio/<tier>`, by default
  `~/.local/share/imbue-studio/<tier>`

- cache (`.uv-cache`, `template-cache`) at `$XDG_CACHE_HOME/imbue-studio/<tier>`,
  by default `~/.cache/imbue-studio/<tier>`

- logs at `$XDG_STATE_HOME/imbue-studio/<tier>/log`, by default
  `~/.local/state/imbue-studio/<tier>/log`

The regenerable bytes now live under the cache base directory, which backup
tools skip and which nothing on the old layout excluded. The directory is
`imbue-studio` -- `package.json`'s `name`, which is also the Linux executable
and the `.deb` id -- rather than the `Imbue Studio` the Apple roots take from
`productName`. The packaged install directory stays `/opt/Imbue Studio`.

Electron's own log path points at the log root. It is the one role that does not
follow `userData` the way `crashDumps` and `sessionData` do, so it otherwise
lands on a directory no layout documents; this corrects macOS too, where the
default omitted the tier.

The first launch after updating moves an existing `~/.minds` onto those roots
and leaves the emptied directory behind, the same one-shot migration macOS runs:
each entry keeps its name, `logs/` is unwrapped into the log root, the SSH-key
paths recorded in mngr's per-host state are rewritten, and generated shell
completions are dropped for mngr to rebuild. A pre-migration workspace's
latchkey token still resolves, through the link left at `~/.minds/latchkey`. A
stale `api.sock` is skipped rather than moved, since the backend rebinds its own.
The move is fail-hard: a tier half on the old root and half on the new is worse
than an app that does not start, so a failure shows a dialog naming what to move
aside, writes `migration-failure.log` to the log root, and retries next launch.

A `minds` command run before that first launch now stops with an error instead
of writing to the new state root, which would have seeded a destination the move
then halts on.

The three roots honor `$XDG_DATA_HOME`, `$XDG_CACHE_HOME` and `$XDG_STATE_HOME`,
reading an unset, empty or relative value as absent -- a padded value such as
`"  /data  "` counts as relative -- and the AppImage's menu entry and icon read
`$XDG_DATA_HOME` by that same rule. `MINDS_DATA_HOME` is now
`STUDIO_ROOTS_OVERRIDE`: it points all three roots at one directory, is set by
test fixtures and `minds-admin demo` rather than by users, and still outranks the
XDG variables. Setting only the retired name raises rather than silently
resolving the real roots.

`minds-admin env list` reads the tiers off the shared state parent on Linux as it
already did on macOS, so a tier whose files have not migrated is not listed until
they have. The `imbue-studio` skill names the Linux state root, and its shell
snippet resolves the root on either platform.

`style_guide.md`'s naming rule now states the line it actually draws: names minds
chooses for itself keep `minds`, and names the OS or the packager derives from app
identity follow `productName`.

`docs/linux-on-mac.md` records how to run the real Linux app from a Mac in a Lima
VM over VNC, the only way to exercise these roots and the migration without a
packaged arm64 build.
