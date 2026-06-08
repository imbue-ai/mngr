- The `POST /hosts/lease` endpoint no longer accepts `preferred_region`. Leases
  are constrained only by the optional hard `region` field (equality match);
  when unset, the lease is region-agnostic.
