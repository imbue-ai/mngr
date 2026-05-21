# Unabridged Changelog - resource_guards

Full, unedited changelog entries consolidated nightly from individual files in the `changelog/resource_guards/` directory.

For a concise summary, see [CHANGELOG.md](CHANGELOG.md).

## 2026-05-20

Project now participates in the per-project changelog layout: a `changelog/` subdirectory holds per-PR entry files, and `CHANGELOG.md` / `UNABRIDGED_CHANGELOG.md` at the project root hold the consolidated history. See the full rationale in `dev/changelog/mngr-changelog-per-project.md`.

Add `@fixture_uses_resources` to `imbue.resource_guards` for declaring resource use at the fixture level. Module/session-scoped fixtures that opt in run their setup and teardown under their own guard scope, so resource calls inside the fixture are authorized against the fixture's declaration rather than the consuming test's marks. Untouched fixtures keep existing behavior.

Adjust the mark semantics around `@fixture_uses_resources`:

- `@pytest.mark.<resource>` on a test is now satisfied by either direct resource invocation in the test body OR by a `@fixture_uses_resources(<resource>)` fixture in the test's closure.
- The mark is now **required** on every consumer of a tagged fixture, even consumers that don't directly invoke the resource. This makes `pytest -m <resource>` the canonical selector for every test that transitively needs the resource, with no silent escape hatch.
- The block check (calls without the mark) is unchanged.
