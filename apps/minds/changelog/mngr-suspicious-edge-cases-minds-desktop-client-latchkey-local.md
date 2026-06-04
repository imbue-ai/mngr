Hardened suspicious edge-case handling in the desktop client's latchkey package:

- File-sharing access-mode rendering (`_access_human_label`) now branches on the
  `FileSharingAccess` enum with `match`/`assert_never` instead of an if-chain
  with a raw-string fallback. An unrecognized mode now crashes loudly rather than
  silently splicing a raw token into the agent-facing grant/deny message.
- The permission-requests stream parser now catches the precise
  `pydantic.ValidationError` (not the broader `ValueError`) when a streamed line
  has an unexpected shape, matching the catalog parser.
- Documented the deliberate fail-closed handling when the gateway returns a
  present-but-malformed `rules` body for a permissions lookup, and the
  unreachable wrong-event-type guard in the file-sharing handler's
  `display_name_for_event`.
