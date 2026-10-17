`mngr` now writes its process title with `shlex.join` instead of joining arguments on a bare space.

The title is set on every invocation and overwrites argv, so it is what `ps` shows and what `psutil.Process.cmdline()` returns -- the only surviving record of how a running `mngr` was invoked. Joining on a bare space made an argument containing a space indistinguishable from two arguments, so the title misreported the invocation it was there to describe.

Visible effect: a path with a space appears quoted in `ps` output, e.g. `--latchkey-directory '/Users/Jane Doe/latchkey'`.
