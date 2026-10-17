`mngr plugin add` and `mngr plugin remove` no longer break a mngr that was installed from a git URL.

Every plugin command rebuilds the whole `uv tool install` command from uv's receipt. The receipt has no structural place for a git ref or a subdirectory -- its only field is `git`, a single URL -- so uv encodes both as query parameters (`<repo>?subdirectory=libs%2Fmngr&rev=<ref>`). uv reads its own serialization back correctly, but it is not a PEP 508 requirement URL, and mngr was re-emitting it as one. uv then silently ignored the ref and the subdirectory and built the repository root, which for a monorepo fails in setuptools with "multiple packages discovered".

Two fixes: existing `--with` requirements are translated into PEP 508 form before being handed back to uv, and the *base* requirement keeps its git source instead of collapsing to a bare `imbue-mngr` (which would have silently re-resolved mngr from PyPI, replacing the pinned commit with whatever version happened to be released).

This is what lets a consumer depend on an unreleased mngr -- pinned to a commit of the public repo -- and still manage its plugins.

The ref is placed immediately after the path, before any surviving query or fragment. Appending it to a fully assembled URL put it after them, where uv ignores it and resolves the default branch instead -- asking for `v0.2.16` installed HEAD, with no error. A fragment the URL already carried is now kept, and a `subdirectory` is merged into it rather than appended as a second `#`.
