Fixed the per-PR changelog enforcement check, which was passing vacuously in CI.

The check previously ran as an acceptance test (`test_pr_has_changelog_entry`) inside the offload Modal sandbox, but the sandbox does a fresh `git init` (so `main == HEAD`) and never fetches `origin`, so its base-branch diff always came back empty and the check passed no matter what. Any PR could merge without changelog entries.

The enforcement now lives in a dedicated CI gate, `scripts/check_changelog_entries.py` (run via the `check-changelog` GitHub Actions job and the `just check-changelog` recipe), which computes the changed-file set against the real base branch on the orchestrator where a base ref actually exists. It refuses to run with a loud non-zero exit if it cannot resolve a diff base distinct from HEAD, so it can never again pass vacuously. The old sandbox-bound acceptance test has been removed.
