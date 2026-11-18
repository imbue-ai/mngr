Fixed: sending a message that is just `?` to a Claude agent no longer fails after a 90-second timeout.

Claude Code reads a lone `?` typed into an empty input box as the key for its shortcuts panel, so the message never reached the input box and the Enter that followed submitted nothing. mngr now pastes a lone `?`, which Claude Code inserts as text. A message starting with `?` followed by more text was already typed correctly and is unchanged.

Confirming a message that has no letters or digits, such as `?`, `??` or an emoji, now accepts Claude recording it. The fallback check looked only for transcript records containing a letter or digit, so it could never find such a message's own record.
