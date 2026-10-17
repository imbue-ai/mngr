Ratchet failure messages now say what to do when a count legitimately has to rise: change only the number, and put the reason in your response and the PR description rather than in a comment. Ratchet files record counts, not history, and the comments explaining past count bumps went stale as soon as the next bump landed.

The 9 such comment lines that had accumulated in `libs/imbue_common/imbue/imbue_common/test_ratchets.py` have been removed.
