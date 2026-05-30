Strengthened the `mngr create --help` e2e test: it now also asserts stderr is
empty and that the `--message` flag (demonstrated in the preceding tutorial
block) is documented. Added a companion test verifying the documented `c` alias
produces help output identical to `mngr create --help`.
