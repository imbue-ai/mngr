Added invitations to the share panel (`specs/inviting-granted-visitors/spec.md`). A granter can invite the person behind a user grant or an email grant; Imbue sends the invitation email on its notification stream, and the row shows what the granter may learn: invited (with when), could not invite, or joined (with the first and latest visit). Nobody at a domain grant is notified, as the panel already says.

- Every save of the grants document while the workspace is published, every publish, and every load of the panel push the parsed document to Imbue Cloud's centralized grants table, which invitations are made from. The sharing status document carries `grants_synced`: false when the push did not land (the next save or load retries), null while unpublished; the panel offers no Invite until the grants are synced.

- Desktop API: `POST /api/v1/workspace-sharing/<id>/invitations` (body `{"email": ...}` or `{"user_id": ...}`, plus an optional `app`) invites one grantee and answers `{outcome, invited_at}` with `outcome` one of `invited`, `could_not_invite`, `over_allowance`, `too_soon`, or 409 `{"error": <code>}` with the connector's refusal (`grants_out_of_date` is retried once after a push, `not_invitable` means the person has already joined). `GET /api/v1/workspace-sharing/<id>/invitation-outcomes` lists each open user or email grant's outcome.

- Granting a person now requests their invitation on its own: there is no separate Invite button for the first send, and the connector still applies the cooldown, the allowance, and the suppression list. Each row's invitation status indicator is one typed value (nothing, inviting, invited with when, could not invite, the refusal a cooldown or an allowance gives, or joined with the visits), so exactly one of them ever shows, with a re-invite offered only when another send can be made. The row clips its own content, so the re-invite never floats outside it.

- Settings: an account's notification email can be turned off and on from the Accounts page (`GET`/`POST /ui/api/accounts/<user_id>/notification-preferences`); the switch governs invitations and other notification email, never the email an account needs such as password resets.

- Corrected the workspace glossary for the invitation rulings of 2026-09-30: the suppression list governs the notification stream only (verification and password-reset mail never consult it), and the email provider is named as Postmark.

- An invitation now carries the workspace's own display name, as the app shows it, rather than the internal name of its primary agent (`system-services` on every workspace), which is what the invitation email had been naming the workspace. A workspace with no display name sends none, and the email says so in words instead of inventing one.

