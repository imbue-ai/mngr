# Changelog - resource_guards

A concise, human-friendly summary of changes for the `resource_guards` library. Entries are categorized using the [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) categories: Added, Changed, Deprecated, Removed, Fixed, Security.

For the full, unedited changelog entries, see [UNABRIDGED_CHANGELOG.md](UNABRIDGED_CHANGELOG.md).

## [Unreleased]

### Added

- Added: `@fixture_uses_resources` decorator for declaring resource use at the fixture level — module/session-scoped fixtures that opt in run setup and teardown under their own guard scope, so resource calls inside the fixture are authorized against the fixture's declaration.

### Changed

- Changed: `@pytest.mark.<resource>` on a test is now satisfied by either direct resource invocation OR by a `@fixture_uses_resources(<resource>)` fixture in the test's closure; the mark is now **required** on every consumer of a tagged fixture, making `pytest -m <resource>` the canonical selector for every test that transitively needs the resource.
- Changed: Project now participates in the per-project changelog layout (per-project `changelog/`, `CHANGELOG.md`, `UNABRIDGED_CHANGELOG.md`).
