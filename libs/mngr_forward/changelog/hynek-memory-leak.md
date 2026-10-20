Fixed unbounded memory growth in `mngr forward`. It supervises a `mngr observe --discovery-only` child plus one `mngr event --follow` child per agent, all of which stream for as long as the forward runs, and it retained every line they ever emitted.

Those children are now started with `is_output_accumulated=False`, so lines are consumed as they arrive and then dropped. Their stderr continues to be logged.
