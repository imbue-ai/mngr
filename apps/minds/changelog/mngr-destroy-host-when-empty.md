The "destroy workspace" UI action now releases the underlying
imbue_cloud-leased host's lease immediately rather than waiting the 7-day
destroyed-host grace period for mngr's GC to run `delete_host`. The
implementation lives in `mngr destroy` (see `libs/mngr/changelog/`);
minds' destroy command was previously *intentionally* not chaining lease
release because the grace-period delegation was the design. That
intentional decision is no longer correct -- `mngr destroy` now drops
cloud-side resources up front, and the grace period only retains
historical state. The stale "Lease release is intentionally NOT chained
here" comment in `destroying.py` is updated to reflect the new contract.
