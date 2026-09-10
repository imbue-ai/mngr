Stopped re-downloading the `ttyd` binary on every `mngr create`.

When `ttyd` was missing, the plugin fetched a 1.36 MB binary from GitHub and then tried to
install it with `sudo`, which cannot succeed non-interactively. The failure was logged as a
warning and nothing recorded that it happened, so the next create downloaded it again --
2.3-4.5 seconds added to every single agent creation, forever, plus a 1.36 MB file orphaned
in `/tmp` per attempt.

The plugin now decides whether the install can succeed *before* downloading anything, and does
the whole thing in one host command instead of two:

- macOS hosts no longer download at all. The upstream release ships Linux binaries only, so
  what was being fetched was a Linux ELF that macOS could never execute. The plugin now says
  so and points at `brew install ttyd`.

- `sudo` is only used when `/usr/local/bin` is not already writable, and only when it works
  without a password. Hosts that have neither are told why, and skip the download.

- Running as root now installs correctly. It previously could not: the shell command
  short-circuited on its own `id -u` test before reaching `curl`, so `ttyd` was never
  installed on a root host and the command always reported failure.

- The download is cleaned up when an install fails, instead of being left in `/tmp`.

On a macOS host without `ttyd`, this takes the per-create cost of the check from 4.5s to 0.1s.
