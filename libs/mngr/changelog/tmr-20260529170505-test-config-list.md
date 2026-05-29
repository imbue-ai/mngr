Strengthened the `mngr config list` e2e test (`test_config_list`) to verify
actual behavior rather than just exit status: it now asserts the merged-config
header is printed and that a value set via `mngr config set` is reflected in the
subsequent merged listing.
