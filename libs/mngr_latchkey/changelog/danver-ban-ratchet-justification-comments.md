Removed the 32 comment lines that had accumulated inside `libs/mngr_latchkey/imbue/mngr_latchkey/test_ratchets.py` explaining why individual ratchet counts had been raised.

A ratchet file records counts, not history. The reason a count moved now goes in the commit message and the changelog entry, the reason a particular code site is allowed goes in that module's docstring, and the set of exempt files goes in the `excluded_patterns=(...)` argument -- all places that stay correct as the code changes, which the comments did not.
