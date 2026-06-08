Refined the cron automation recipes doc (`docs/cron_recipes.md`):

- The agent-spawning recipes (`warm-window.sh`, `dispatch-task.sh`) now `cd
  "$PROJECT_DIR"` before creating an agent, using a placeholder project path.
  cron starts in `$HOME` (usually not a git repo), and mngr resolves
  project-scoped config from the cwd's git worktree root -- so running inside
  the project is what gives the new agent a git root to branch from and applies
  the project's settings (`create_templates`, labels, etc.). Dropped the now
  redundant `--from ":$PROJECT_DIR"` from `dispatch-task.sh` (cd makes the
  create source default to the project's git root). Clarified that the
  `warm-window.sh` warmer does no real work, so its `PROJECT_DIR` can be any git
  repo already trusted in Claude Code -- the project context is irrelevant, and
  `--no-connect` can't answer the trust prompt on first use.
- Reworked the Scheduling section: the `PATH` note now covers both the Linux
  (`/usr/bin` via apt) and macOS (`/opt/homebrew/bin` via Homebrew) dependency
  locations around a single cron example.
- Added a macOS LaunchAgent section as the recommended alternative to `cron` on
  macOS. cron jobs run outside the GUI (Aqua) login session and so can't reach
  the login Keychain, where Claude Code stores its credentials -- cron-launched
  agents come up "Not logged in". A user LaunchAgent loaded into the Aqua
  session has Keychain access and authenticates normally. Includes a plist
  skeleton (`StartInterval`, `EnvironmentVariables` PATH, log paths),
  `launchctl bootstrap`/`bootout` load/unload commands, and the
  runs-only-while-logged-in tradeoff.
