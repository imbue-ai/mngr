# Changelog - resource_guards

A concise, human-friendly summary of changes for the `resource_guards` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

## [v0.1.8] - 2026-06-05

## [v0.1.7] - 2026-05-28

### Added

- Added: `@fixture_uses_resources` decorator for declaring resource use at the fixture level. Module/session-scoped fixtures that opt in run their setup and teardown under their own guard scope so resource calls inside the fixture are authorized against the fixture's declaration.

### Changed

- Changed: `@pytest.mark.<resource>` on a test is now satisfied by either direct invocation OR a `@fixture_uses_resources(<resource>)` fixture in the test's closure; the mark is now **required** on every consumer of a tagged fixture so `pytest -m <resource>` is the canonical selector.
