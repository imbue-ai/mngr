Fixed: a message starting with `?` sent to an Antigravity agent no longer opens agy's help screen instead of arriving.

Typed into an empty input box, agy reads `?` as its help key, and the help screen then swallows the rest of the message. A lone `?` failed after the 90-second confirmation timeout, and agy kept at most the first letter of a longer message like `?what changed`. mngr now pastes any message starting with `?`, and agy inserts it as text.
