Bump Latchkey to 3.17.0.

Connecting a Google account (Gmail, Google Docs, ...) through the permission dialog or the settings page now fails, with a message naming the permissions that were not granted, when some of the consent checkboxes are left unticked. Previously the partial credential was stored and the agent's requests failed later. Other services are unaffected.
