`scripts/push_vault_from_file.py` now writes each declared key as its own single-`value` leaf at `secrets/minds/<tier>/<service>/<KEY>` (the new "split" Vault secret layout) instead of a single flat KV entry with many fields.

Added `scripts/remove_old_flat_vault_secrets.py`, a one-off cleanup tool that deletes the old flat per-service Vault entries for a tier (`secrets/minds/<tier>/<service>`) once they have been mirrored into the split layout. It refuses to delete any entry whose split mirror is missing or whose keys/values disagree, defaults to a dry-run, and requires `--yes` to actually delete.

`scripts/changelog_deploy.sh` now reads its `GH_TOKEN` / `ANTHROPIC_API_KEY` from the split layout (`secrets/mngr/dev/github/GH_TOKEN` and `secrets/mngr/dev/anthropic/ANTHROPIC_API_KEY`, value under `.data.data.value`) instead of the old flat entries.
