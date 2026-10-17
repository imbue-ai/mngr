The pool bake now waits on the browser env.d unit's satisfied condition (`test -x /opt/fortress/tilion-fortress/tilion`) instead of the retired deferred-install marker files, and the slice bake bakes the browser layer by running the exact `scripts/env.d/1000-playwright-fortress.sh` unit the workspace runs at boot -- loaded slices hit the unit's fast satisfied-check with no marker involved.

- Pool-host bakes pass `MNGR_HOST_DIR=/home/user/.mngr` (the workspace layout's container-internal host_dir) to baked hosts instead of the legacy `/mngr`.
