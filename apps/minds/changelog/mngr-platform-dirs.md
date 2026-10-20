Minds now stores its files where macOS expects them, instead of all in one hidden `~/.minds` folder. Your existing data moves automatically the first time you launch this version; nothing is deleted, and you should not have to sign in again.

Each env (production, staging, your dev env) gets three directories instead of one:

- `~/Library/Application Support/Imbue Studio/<env>/` -- sessions, keys, agent state, the Python environment

- `~/Library/Caches/Imbue Studio/<env>/` -- caches that can be rebuilt from scratch

- `~/Library/Logs/Imbue Studio/<env>/` -- logs

Why it is worth doing: `~/.minds` reached about 3.8 GB on a working install, roughly half of it regenerable cache, and because it sat in your home directory Time Machine backed up every byte. macOS excludes `~/Library/Caches` and `~/Library/Logs` from backups automatically, so filing those files correctly takes around 1.5 GB out of every backup. Your application data is also now reachable in Finder (Go -> Library); `~/.minds` was hidden, so Finder would not show it to you at all.

Two things this fixes along the way. Crash reports and queued error reports were being written outside your env's directory and shared across every env. And `rm -rf ~/.minds` to clear a cache also destroyed your saved sessions and SSH keys -- the three roots can now be cleared independently.

The old `~/.minds` folder is left behind, emptied, so you can see where things used to be; delete it once you are satisfied everything works. Anything that recorded a path into the old folder is rewritten as it moves, so your agents' SSH keys keep working. `mngr` shell completion is the exception: it is regenerated rather than rewritten, so tab completion returns the first time `mngr` reinstalls it. If the move fails partway, the app refuses to start rather than running with your files split across two layouts; it writes what went wrong to `~/Library/Logs/Imbue Studio/<env>/migration-failure.log`, shows it to you, and relaunching retries the move.

This change is macOS-only. On every other platform the app keeps the single `~/.minds` folder exactly as before, down to each file's path, so nothing moves and there is nothing to migrate. The XDG layout Linux would want is a separate change, and it needs a Linux launch test first: nothing today runs the app on Linux, so a migration there would move real data on a path no test has ever exercised.
