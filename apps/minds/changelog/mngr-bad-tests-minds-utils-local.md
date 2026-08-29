Strengthened the unit tests for `imbue.minds.utils` logging and output helpers:

- `_format_user_message` tests now assert the exact colored format string (via inline-snapshot) for every level instead of loose substring checks, so a dropped/swapped DEBUG/TRACE/WARNING/ERROR color branch is now caught.
- Added `setup_logging` cases that verify messages below the configured threshold are suppressed (e.g. DEBUG hidden at INFO, INFO hidden at WARN), covering the level-filtering behavior that was previously untested.
- Replaced the fragile "no brace before the marker" heuristic with an exact stderr equality assertion for the human format.
- Replaced the bespoke `_FakeLevel` record fake with `types.SimpleNamespace`.
- `emit_event` HUMAN-without-message test now also asserts stderr stays empty.
