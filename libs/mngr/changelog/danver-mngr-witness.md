List the new `imbue-mngr-witness` package (`libs/mngr_witness`, the behavior-corpus witness pipeline) in `UNPUBLISHED_PACKAGES`, so the release tooling knows it is internal and does not offer it for publication, and add the generated help page for its `mngr witness` command under `docs/commands/secondary/`. No behavior change to `mngr` itself.

Remove the generated help page for the retired `mngr tmr-behaviors` command.
