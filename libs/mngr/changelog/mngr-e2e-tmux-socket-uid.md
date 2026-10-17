The release test covering `mngr create -w` (extra tmux windows for a command agent) now finds the harness's tmux socket when the tests run as any user.

It queried `$TMUX_TMPDIR/tmux-0/default`, but tmux names that directory after the running user's id, so the path existed only when the tests ran as root. Everywhere else the query failed with "error connecting ... No such file or directory" and the test could not check that the extra window was created. `mngr create -w` itself is unchanged.
