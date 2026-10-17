Fixed the tutorial release tests, which failed on stale assumptions rather than on real defects.

`test_create_docker_default_image` confirmed its agent with `mngr list --include 'host.provider == "docker"'`. `--include` filters what discovery returned; it cannot stop discovery, and `mngr list` defaults to `--on-error abort` across every enabled provider, so an enabled-but-unreachable one (Modal, in a job holding no token) aborted the listing with exit 6 before any agent was reported. It now scopes with `--provider docker`, which the rest of the file already uses.

Four `mngr exec` calls passed their command as loose tokens. `mngr exec` takes `[AGENTS]... COMMAND`, so the command must be a single argument -- the tutorial's own `test_exec` docstring says so. Unquoted, the leading word is consumed as another agent name: `mngr exec my-task cat /etc/os-release` looked up agents `cat, my-task`, and `mngr exec my-task -- sh -c '...'` rejected `-c` as an agent name.

The custom-Dockerfile tutorial documented a command that cannot work: `-b file=./Dockerfile.dev`. The docker provider passes `-b` values to `docker build` verbatim, so that produced two positional arguments and docker rejected it with `'docker build' requires 1 argument`. The Dockerfile is selected with docker's own `--file`.

Four tests declared a resource they never touch -- `@pytest.mark.modal` on a listing with no agents, `@pytest.mark.rsync` on a dry run and on local in-place agents -- which the resource guards reject with "marked with X but never invoked X".
