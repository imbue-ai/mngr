The ownership lock is now the only record a `mngr latchkey forward` keeps.

`latchkey_forward.json` and everything that read it are gone. It existed to name a forward from a build that predates the lock -- such a forward holds none, so nothing lock-based can see it -- and the first launch of the release that introduced the lock replaced every one of them. With no pre-lock forward left to find, the record has nothing to say.

Removed with it: the module that recognised such a forward by its process title, the check that refused to start beside one, and the `LatchkeyForwardInfo` type. Nothing in the package reads a process title any more, for any purpose.

This must not ship before the release that introduces the ownership lock has reached users. Until then `latchkey_forward.json` is the only thing that can find a forward predating it.
