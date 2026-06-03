The `BaseMngrError` base class has been removed entirely. `MngrError` now inherits directly
from `click.ClickException`, and every mngr error inherits from `MngrError`. There is no longer
a separate non-user-facing error tier: all mngr errors render as a clean `Error: ...` message
at the CLI (plus any help text) rather than a traceback. This is a no-op for users -- prior
commits had already moved every error class under `MngrError`; removing `BaseMngrError` simply
finalizes that consolidation.

`except` clauses that listed an error type already covered by another type in the same clause
were collapsed (e.g. `except (MngrError, UserInputError)` -> `except MngrError`,
`isinstance(e, (MngrError, BaseMngrError))` -> `isinstance(e, MngrError)`). Clauses pairing
`MngrError` with unrelated types (`OSError`, `docker.errors.*`, etc.) are unchanged.
