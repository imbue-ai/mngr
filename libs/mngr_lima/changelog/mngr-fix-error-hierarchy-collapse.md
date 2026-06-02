Collapsed a redundant `except` clause: `except (LimaCommandError, MngrError, OSError)` is now
`except (MngrError, OSError)` (since `LimaCommandError` is already a `MngrError` subclass). No
behavior change.
