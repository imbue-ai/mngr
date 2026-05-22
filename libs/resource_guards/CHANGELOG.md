# Changelog - resource_guards

A concise, human-friendly summary of changes for the `resource_guards` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `@fixture_uses_resources` decorator for declaring resource use at the fixture level — module/session-scoped fixtures that opt in run setup and teardown under their own guard scope, so resource calls inside the fixture are authorized against the fixture's declaration rather than the consuming test's marks.

### Changed

- Changed: `@pytest.mark.<resource>` on a test is now satisfied by either direct invocation in the test body OR by a `@fixture_uses_resources(<resource>)` fixture in the test's closure; the mark is required on every consumer of a tagged fixture so `pytest -m <resource>` is the canonical selector.
- Changed: Adopted per-project changelog layout (`changelog/` dir, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md` at the project root).
