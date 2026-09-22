# Containers resolve their outer host as `host.docker.internal`

`run_container` now creates every container with
`--add-host host.docker.internal:host-gateway` (`OUTER_HOST_ADD_HOST_ARGS`, built
from `imbue.mngr.primitives.OUTER_HOST_HOSTNAME_IN_CONTAINER`), so a service the
VPS binds on its docker bridge address is reachable from inside the container by
that name without an address known at create time. `mngr_latchkey` uses it to
point a VPS workspace at the VPS-resident gateway directly, replacing the
VPS->container reverse SSH tunnel. Containers created before this change are
unaffected (the mapping cannot be added to an existing container);
`mngr_latchkey` keeps the tunnel for those.
