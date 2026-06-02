A logging test that imported `BaseMngrError` from `imbue.mngr` (now removed) no longer reaches
into the `mngr` package: it uses a local test-only exception instead. No runtime behavior change.
