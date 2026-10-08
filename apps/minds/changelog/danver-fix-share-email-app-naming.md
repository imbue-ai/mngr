An invitation email now names a shared app the way the share panel does.

The desktop already resolves each share target's display name from the workspace's service registry for the panel; it now sends that name with the invitation, so mail about a shared app says `the app "World of Nonsense"` rather than `the world-of-nonsense app`, which was the hostname label behind it. An app that registered no display name is still named by its service name, and a whole-workspace share is named for the workspace, never after the shell app that serves it.

Inviting someone now needs your own email address verified, since the invitation is mail Imbue sends in your name. The share panel reports the refusal as a prompt to verify your address and retry; publishing, granting and every other part of sharing are unaffected.
