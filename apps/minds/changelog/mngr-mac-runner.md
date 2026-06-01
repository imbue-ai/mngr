# Self-hosted Mac runner support

- Added `apps/minds/scripts/mac-runner-reset.sh`: cleans `~/.minds`, removes the installed `.app`, kills leftover Minds processes, and stops/deletes any Lima VM instances. Optionally re-downloads + installs a fresh `.app` from a ToDesktop `.zip` URL passed as the first argument. Intended to run at the start of every verification job on the dedicated self-hosted mac-runner so each run starts from a known-clean state. Preserves only the Lima base-image cache (`~/Library/Caches/lima/`), which is ~1.5 GB and unrelated to Minds itself.
