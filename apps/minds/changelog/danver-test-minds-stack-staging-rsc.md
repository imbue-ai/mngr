The desktop app no longer sends a verification email of its own when a cloud create is held on an unverified address, and no longer claims an email was sent when it was not.

The connector now sends the link when the account is created, so the create form and the first-run start flow stop originating a duplicate send that only burned the cooldown the user's own "Resend" press needs.

Both surfaces used to state "the link we sent to <address>" whatever had happened, and to report a resend that never went out as "an email was sent a moment ago". A resend now reports which of three things occurred -- sent, suppressed because one went out moments ago, or failed -- and only the first two point the user at an inbox.
