- Fixed `minds pool {list,create,destroy}` leaking the Neon pool DSN (which
  embeds the DB username + password) into the `Running: ...` log line whenever
  `--database-url` was passed explicitly. The DSN is now masked before the
  command is rendered for logging; the real subprocess still receives the
  unredacted value. The secret-masking logic that `mngr forward`'s
  `--preauth-cookie` redaction already used is now a shared
  `imbue.minds.utils.secret_redaction.redact_secret_flag_values` helper.
