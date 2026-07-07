Fixed and hardened the `just minds-stop` recipe.

It depended on `pstree` (not installed on stock macOS); the `pstree | grep` pipeline returned no match and aborted the whole recipe under `set -e`, so `minds-stop` always failed. It now walks the process tree with `ps` (portable, no `pstree`).

It also now stops the desktop app launched via LaunchServices (`open`) on macOS, which is reparented to launchd and thus outside the recipe's own process tree: the recipe finds the Electron main process by its unique per-worktree app-dir argument, then SIGTERMs both it and the recipe tree (concurrently + CSS watcher + launcher), waits a grace period, and SIGKILLs any specific survivors so a stalled graceful shutdown cannot leave the app or its Python backend running.
