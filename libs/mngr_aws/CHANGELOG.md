# Changelog - mngr_aws

A concise, human-friendly summary of changes for the `mngr_aws` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Changed

- Changed: `allowed_ssh_cidrs` is now typed `ScalarStrTuple` so a developer's `settings.local.toml` tightening it to a single CIDR cleanly replaces the default; previously the narrowing guard rejected this and broke every command for anyone tightening the value. The default remains `["0.0.0.0/0"]`.
