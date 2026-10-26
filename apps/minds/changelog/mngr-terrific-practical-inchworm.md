- `mac-runner-reset.sh` deletes the runner's `minds-host-*` Lima instances with `limactl delete --force` instead of the `--all` flags Lima rejects (logging limactl's error when a delete fails), and clears the `/tmp` diagnostic directories the workflow uploads, so a run's artifact no longer carries an earlier run's probe log or screenshots.

- The launch-to-msg e2e's pre-run sweep removes `/tmp/minds-electron-relaunch.log` beside `/tmp/minds-electron.log`, so a run that fails before the relaunch step does not leave an earlier run's relaunch log for the artifact.
