Renamed the changelog tooling scripts so they all share a `changelog_` prefix
and sort together: `consolidate_changelog.py` -> `changelog_consolidate.py`,
`trigger_changelog_consolidation.py` -> `changelog_consolidation_trigger.py`,
and `setup_changelog_agent.sh` -> `changelog_deploy.sh` (all internal imports,
docstrings, and the consolidation prompt updated to match).

Added a `changelog-deploy` justfile recipe that wraps
`scripts/changelog_deploy.sh`, so redeploying the nightly
changelog-consolidation schedule is `just changelog-deploy` instead of invoking
the script by path.
