Defined and standardized the "SSH protocol banner" terminology that the provider SSH code leans on throughout but never pinned down (MIND-203). This is a comments-only change with no behavioral effect; the banner-read connect retry it documents was fixed separately under MIND-202 (#572).

The term now has a plain-language definition at its anchor, `SSH_BANNER_TIMEOUT_SECONDS`: the SSH protocol banner is the server's RFC 4253 identification string (the `SSH-2.0-...` line sent immediately after the TCP connect, before key exchange), which is distinct from the RFC 4252 userauth banner it is easily confused with. The incidental variants ("banner window", "banner exchange", "banner reset") now refer to it consistently.

Also locks in the `imbue/mngr` inline-imports ratchet count (4 -> 3) to match the current tree.
