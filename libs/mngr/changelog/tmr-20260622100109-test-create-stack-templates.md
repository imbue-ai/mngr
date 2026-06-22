Strengthened the `mngr create --template` template-stacking e2e tests:

- The happy-path stacking test now verifies the stacked `transfer=none` template's concrete effect (the agent runs in-place in the session cwd) instead of only checking the command's exit code.

- Added a test that exercises the tutorial's "later templates override earlier ones" behavior: two templates set a conflicting `transfer` value and the test confirms the later template wins.
