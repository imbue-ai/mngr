Hardened over-defensive edge-case handling across the imbue_cloud plugin, so
silent-wrong-output paths now fail loudly or are explicitly documented as
correct.

- Auth refresh: when the connector returns a rotated refresh token in an
  unexpected (non-string) shape, `_refresh_locked` now raises
  `ImbueCloudAuthError` instead of silently reusing the old, already-consumed
  token (which would trip SuperTokens token-theft detection on the next refresh
  and revoke the whole session family). The refresh now also requires
  `status == "OK"` explicitly rather than treating a missing status as success.
  A null refresh token (SuperTokens did not rotate this cycle) still correctly
  keeps the current one.

- Connector list endpoints (`list_hosts`, `list_litellm_keys`, `list_tunnels`,
  `list_services`, `list_buckets`, `list_bucket_keys`, paid-list reads) no
  longer coerce a non-list body to `[]` or silently drop unparseable rows.
  A non-list body, a non-dict entry, or any single unparseable/empty-name entry
  now raises the endpoint's typed error via a shared `_parse_connector_list`
  helper. The tunnel/service/paid-list parsers raise rather than default a
  missing identity name to `""` (which would otherwise target the wrong URL
  path segment in later delete/auth operations). NOTE: this is deliberately
  aggressive -- one malformed row fails the whole list call -- and we may want
  to soften it to warn-and-skip if connector schema drift proves common.

- Adopt path (`host.py`): `create_agent_state` now hard-requires the bake's
  agent `type` (a defaulted "claude" could silently mistype the agent and drive
  provisioning down the wrong path), matching the existing hard-require on
  `name`. The `work_dir` fallback to the computed path is retained and
  documented (it mirrors `create_agent_work_dir`'s deliberate tolerance), and a
  fabricated `create_time` now emits a warning so a corrupt bake `data.json` is
  visible.

- Listing details: `_build_agent_details_from_raw` now warns when an agent's
  `type`/`command` are missing from the listing data and documents that the
  substituted values are display stand-ins, not real configured values.
  `get_host_resources` now accepts a float `cpus` attribute (previously an
  `int`-only check silently fell back to 1 for a JSON float like `4.0`), and its
  unknown-host default is documented as the deliberate "limits unknown" answer.

- `LeasedHostInfo.leased_at` is now `str | None` (default `None`) instead of a
  required timestamp that the fresh-lease synthesis path filled with an invalid
  empty string.

- The `tunnels set-auth` CLI no longer carries a dead `None`-policy guard:
  `_parse_policy_arg` was split into a non-optional `_parse_policy_json` (for the
  required positional) and a `_parse_optional_policy_json` wrapper (for the
  optional `--policy` flag), so the type checker proves the value present.

- Documented (not changed) the deliberate empty-`container_state` -> `CRASHED`
  mapping in `_derive_host_state_from_raw`, explaining why `HostState.UNKNOWN`
  (reserved for unreachable providers) does not fit when the outer host was
  reachable.
