# Live forward owner record readable in one call

New `live_forward_owner()` in `forward_supervisor.py` returns the
`LatchkeyForwardOwner` record published by the forward that currently *holds* a
latchkey directory, or `None` when nobody holds it. It is the record-shaped
counterpart to `owning_forward_process()` and reads the directory once.

The record beside the ownership lock outlives the forward it names (it is
replaced only when the next forward claims the directory), so `load_forward_owner()`
alone cannot tell a running forward from one that was terminated. Consumers
waiting for the shared gateway's bound port -- the Minds desktop client -- use
this instead, which is what stops a supervisor restart from reading as a dead
supervisor.
