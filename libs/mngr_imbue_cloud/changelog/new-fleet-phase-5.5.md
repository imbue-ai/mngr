The pool-host lease client declares `max_box_generation` (the highest slice-fleet box generation this release can operate, currently 2) on every lease, fast and slow path alike, so the connector routes it to rows it can run.

The gen-2 restore-reserve script's cutover-only fixed-ports variant is removed (the incremental migration reserves freshly picked free ports); the supplied-user-data variant stays and the default rendering is byte-identical.
