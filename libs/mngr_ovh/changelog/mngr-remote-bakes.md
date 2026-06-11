Fixed a concurrency bug in OVH VPS recycling where multiple simultaneous `mngr
create`s could all claim the **same** cancelled VPS. The IAM-tag lock wrote the
claimant's value and then re-read it immediately; since OVH IAM tags are
last-write-wins with no compare-and-set, each racing claimant read its own
freshly-written value back and all believed they held the lock (observed live:
4 concurrent pool bakes all recycled one VPS while the other candidates went
untouched).

The claim now waits for the lock-tag writes to converge before checking
ownership, so among N simultaneous claimants exactly one wins and the rest
detect the loss and move on to the next candidate. The convergence wait is
injectable so it can be driven deterministically in tests.
