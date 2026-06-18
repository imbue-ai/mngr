# For tmux users

## Nested tmux

Mngr runs your agents in tmux sessions.
If you already use tmux to run `mngr` itself,
by default, `mngr` won't be able to drop you into the agents' tmux sessions,
because `tmux` refuses to run inside `tmux` by default.

There are two approaches to solve this:

- If you prefer to keep the agents' tmux sessions outside the session where you run `mngr`,
  you can use an alternative `connect-command` to the `create` and `start` subcommands,
  which can, for example, open a new terminal tab and connect to the agent session from there.

  In particular, if you use iTerms2, there's a builtin plugin to do that for you -
  run `mngr plugin list` to see it.

- You can tell `mngr` to allow nested tmux -
  it should have printed a command to do so.

When using nested tmux,
you'll need some configuration to make the keybindings work for both the "outside" and "inside" sessions.
There are several approaches:

- In tmux's default binding,
  pressing `Ctrl-B` twice sends `Ctrl-B` to the program running inside tmux.

  This means you can use all your prefixed keybindings simply by pressing an extra `Ctrl-B` every time.

- You can also configure an alternative keybinding for tmux sessions created by `mngr`,
  by editing `~/.mngr/tmux.conf`.

- A slightly more advanced approach is to have a key that swaps the outer tmux's key table,
  effectively making it switch between which layer of tmux you want to operate on.
  For example, to use F12 for this purpose, put the following in your `~/.tmux.conf`:

  ```
  bind -T root F12  \
    set prefix None \;\
    set key-table off \;\
    set status-style "fg=colour245,bg=colour238" \;\
    refresh-client -S

  bind -T off F12 \
    set -u prefix \;\
    set -u key-table \;\
    set -u status-style \;\
    refresh-client -S
  ```

You can find other approaches by searching for "nested tmux" or "tmux in tmux".

## mngr's tmux sessions are isolated from yours

`mngr` runs its agents on its own private tmux server (a dedicated socket under
`<host_dir>/tmux`, selected via `TMUX_TMPDIR`), separate from your default tmux
server. This keeps `mngr`'s server-global tmux options and key bindings off your
own hand-started sessions, and your `tmux ls` never shows `mngr`'s agent
sessions. No configuration is needed.

To see or attach to an agent's session directly, point `tmux` at the same
server (the socket lives under your mngr host dir, `~/.mngr/tmux` by default):

```bash
TMUX_TMPDIR=~/.mngr/tmux tmux ls
```

(Normally you would just use `mngr connect`, which targets the private server
for you.)

### Overriding the socket location

Set `MNGR_TMUX_TMPDIR` to put `mngr`'s tmux socket somewhere other than
`<host_dir>/tmux`. The main reason to do this is the unix-socket path length
limit (~104 characters on macOS): if your `host_dir` is very deep, the derived
socket path can overflow it, and tmux silently falls back to your default
server. Point `MNGR_TMUX_TMPDIR` at a short directory (e.g. `/tmp/mngr-tmux`) to
avoid that.

### Upgrading from an older mngr

Older versions of `mngr` ran agents on your default tmux server. After
upgrading, `mngr` looks only at its private server, so any agents that were
already running still have their sessions on your default server and now show up
as `STOPPED` in `mngr list` (and `mngr connect` can no longer reach them). This
is a one-time transition.

To migrate a stranded agent, reattach to its old session directly to wrap up or
save its work:

```bash
tmux attach -t =mngr-<agent-name>   # uses your default server
```

then start a fresh agent so it runs on the private server. Agents you create
after upgrading always use the private server automatically.
