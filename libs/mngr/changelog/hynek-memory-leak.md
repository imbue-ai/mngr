Fixed unbounded memory growth in `mngr observe`. It supervises a `mngr observe --discovery-only` child plus one `mngr event --follow` child per host, all of which stream for as long as the observer runs, and it retained every line they ever emitted.

Those children are now started with `is_output_accumulated=False`, so lines are consumed as they arrive and then dropped. Their stderr was previously discarded silently; it is now logged at debug level, so a failing child is still diagnosable.
