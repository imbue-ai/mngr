Strengthened several weak or fragile unit tests under `imbue/mngr/utils` so they actually catch regressions:

- `build_cel_context` tests now assert the converted CEL value types (`StringType`, nested `MapType`) and exact values, instead of merely checking that keys are present. They now catch a regression that stopped converting raw values to `celpy.celtypes`, which would break dot-notation filtering.
- The `imbue.mngr` import smoke test now asserts the package name and that the `cli` console-script entrypoint is a real Click command, instead of the tautological `assert mngr`.
- `get_current_branch` test now checks out a deterministic, uniquely-named branch and asserts the exact returned name, so it would fail if the function returned a commit-ish or remote ref rather than the branch name.
- `check_bash_version` test now exercises the version-comparison branch (an unreachable `minimum=999` returns `False`) instead of only asserting the return type.
- `_format_arg_value` complex-object test now asserts the exact rendered repr via an inline snapshot, catching dropped fields or changed quoting.
- The asciinema cast-player init-script test now verifies the script wires `AsciinemaPlayer.create(...)` to the correct player div id (`player-0`) and runs inside the `DOMContentLoaded` handler, rather than just checking for substrings.
- Editor test sleep scripts now use large, globally-unique durations to avoid leak-detector collisions, with clarifying comments about why the long sleeps make the synchronous `is_running()` assertions race-free.
- The name-generator uniqueness tests are now deterministic (seed the RNG and restore its state, asserting the exact unique-name counts) instead of using a probabilistic `>= 5` threshold.
- Relocated the name-generator tests from `test_name_generator.py` (integration-test naming) to `name_generator_test.py` (unit-test naming) to match their actual nature and the `_test.py` convention.
- Removed two tautological dataclass/enum tests (`InstallMethod` field round-trip and `DependencyCategory` member values) whose behavior is already covered by the behavioral install-command tests; added a comment to the logging-suppressor buffering test explaining why its count bound is `>=`.
