- Recorded the minds 0.8.0 release in `docs/deploy/history/minds-v0.8.0.md`: the fast-path cut, the staging and production deploys that applied migrations 050 to 052 and turned the waitlist on, the bakes on both tiers, and the alpha promotion (mac and web; Linux held on 0.7.4 because the renamed `.deb` package cannot update a `minds` install).

- Moved the Imbue Studio cutover's finished server stages from `next_deploy.md` into `docs/deploy/history/rollouts/imbue-studio-cutover.md`, and reset `next_deploy.md` for the next release: the `.deb` package rename, the two open macOS update-install fixes (PRs #1256 and #1357), the rest of Stage F including the real upgrade check, and the device-login and invite-link checks the new deploys owe.

- The app-release runbook now makes the bundle-lock diff a standing pre-tag check (kept on the fast path too), and says to check the `.deb` package name before listing `linux` on a channel; it no longer says stable stays mac-only.
