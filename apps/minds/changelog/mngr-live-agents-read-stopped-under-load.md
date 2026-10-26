- The backup, restore and apply-update gates now count a chat whose state mngr could not read (UNKNOWN) as running. On a loaded workspace a live chat can read UNKNOWN, and treating it as stopped would let a restore or an update run underneath it.

- The update watchdog no longer counts an update lead or worker whose state reads UNKNOWN as idle, so it stops trying (and failing) to remind a lead that may be mid-turn on a loaded workspace.
