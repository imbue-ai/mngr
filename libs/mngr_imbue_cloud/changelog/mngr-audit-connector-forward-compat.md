Made the connector client forward compatible with newer server deployments.

All connector-response models now inherit new tolerant bases (`WireModel` ignores unknown response fields; `WireEnum` coerces unrecognized wire values to an `UNKNOWN` member), live in a dedicated `wire_types.py`, and are parsed through `validate_wire` / `parse_wire_entries` -- so an additive server change never breaks an already-shipped client, while removed/renamed required fields still fail loudly.

List endpoints no longer degrade to a silently empty listing: one unparseable entry is skipped with a warning, but a non-empty listing whose entries all fail (or a non-list body) raises, so a schema break can never masquerade as "zero workspaces".

A workspace whose lifecycle status the client does not recognize maps to `HostState.UNKNOWN` (shown but not actionable); starting it is refused with an "update the app" message.

Every connector call now carries the canonical `X-Imbue-Client` identification header (mirrored into `User-Agent`), and the connector's structured HTTP 426 "client too old" refusal is surfaced as a typed error.

Workspace-record sync understands the new `record_format` write-lock: records written at a newer format are read-only client-side, and the server's `record_format_too_new` 409 maps to a typed error.

The old tunnel-era compat fields on the account models are gone (tolerant parsing makes them unnecessary), and a project ratchet keeps all response parsing routed through the tolerant entrypoints.
