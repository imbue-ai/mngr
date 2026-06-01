User-visible: minds workspaces running on docker-on-VPS hosts can now be
backed up off-site (restic) when a backup provider is selected at creation
time; the outer-trigger btrfs snapshot path these hosts use is what the
backup service reads from.

(No code change in this project in this PR; the integration lives in the
minds app and the forever-claude-template `host_backup` service.)
