The `minds-launch-to-msg` e2e workflow now sets `MINDS_DATA_HOME=$RUNNER_TEMP/minds`
so the whole run is self-contained under one throwaway tree, independent of the
self-hosted runner's home-dir state from past runs. Diagnostic-artifact
collection reads from the new platform-canonical roots, and the runner-reset
diagnostics list them.
