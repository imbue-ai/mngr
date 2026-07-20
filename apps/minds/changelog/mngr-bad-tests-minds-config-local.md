Hardened the `imbue/minds/config` tests and the small bits of code they exercise:

- `parse_agents_from_mngr_output` now raises `MalformedMngrOutputError` (instead of
  leaking a raw `json.JSONDecodeError`) when the first non-empty mngr output line
  starts with `{` but does not parse, matching how the other malformed-output cases
  are surfaced. Added coverage for blank/whitespace-only stdout returning `[]`.
- `bundled_client_config_path_or_none` accepts an optional `bundled_dir` so both the
  present and absent cases are tested against a temp directory rather than depending
  on ambient repo state.
- Collapsed the duplicated dev/ci deploy-config round-trip tests into one
  parametrized test (extended to staging/production) and tightened a loose
  `workspace_dir` path assertion to exact equality.
