# For tmux users

## Nested tmux

Mng runs your agents in tmux sessions.
If you already use tmux to run `mng` itself,
by default, `mng` won't be able to drop you into the agents' tmux sessions,
because `tmux` refuses to run inside `tmux` by default.

There are two approaches to solve this:

- If you prefer to keep the agents' tmux sessions outside the session where you run `mng`,
  you can use an alternative `connect-command` to the `create` and `start` subcommands,
  which can, for example, open a new terminal tab and connect to the agent session from there.

  In particular, if you use iTerms2, there's a builtin plugin to do that for you -
  run `mng plugin list` to see it.

- You can tell `mng` to allow nested tmux -
  it should have printed a command to do so.

When using nested tmux,
you'll need some configuration to make the keybindings work for both the "outside" and "inside" sessions.
There are several approaches:

- In tmux's default binding,
  pressing `Ctrl-B` twice sends `Ctrl-B` to the program running inside tmux.

  This means you can use all your prefixed keybindings simply by pressing an extra `Ctrl-B` every time.

- You can also configure an alternative keybinding for tmux sessions created by `mng`,
  by editing `~/.mng/tmux.conf`.

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

## Isolating mng's tmux sessions

By default, `mng` creates tmux sessions on your default tmux server. This means `mng`'s sessions will show up alongside your personal sessions in `tmux ls`, and could interfere with your own tmux workflow.

To keep `mng`'s tmux sessions isolated from your own, set `TMUX_TMPDIR` to give `mng` its own tmux server:

```bash
TMUX_TMPDIR="/tmp/mng-tmux" mng create my-agent
```

Your normal `tmux ls` will no longer show `mng`'s sessions, and you won't run into nested tmux issues.

Note: the directory must already exist or tmux will silently connect to the normal server instead.
