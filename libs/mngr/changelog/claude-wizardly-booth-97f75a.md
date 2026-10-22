`SendFailureKind` gains two kinds, so a client of the message API can tell a send that typed nothing from one whose text may already be with the agent:

- `agent_not_found`: the agent was not where the caller located it. Its host has no such agent, or the host could not be reached (offline, would not start, or could not load its agents). Nothing was typed, so looking the agent up again and resending is safe. These failures were previously `unknown`.

- `submission_unconfirmed`: the message was typed and Enter sent, but no evidence confirmed the agent took it within the confirmation window. The text may already be in the agent's input or turn, so sending it again can deliver it twice. This was previously `unknown`.

The Minds chat app falls back to a full discovery and resends whenever a send to an agent's known location fails. With these kinds it can resend only after `agent_not_found`, instead of retyping a message that an unconfirmed submission may already have delivered.

When `mngr create` (with an initial message) or `mngr start` (with a resume message) cannot confirm the agent took the message, the error no longer says the message was NOT delivered and tells you to resend it. It says the message may already be in the agent's input and to check with `mngr connect` before resending. The error raised from these paths also keeps the failure's kind instead of reporting `unknown`.

`mngr message --help` no longer says every non-zero exit other than 7 means the message was not delivered. When the error says no submission evidence appeared, the message was typed but not confirmed and may already be in the agent's input, so check before resending.
