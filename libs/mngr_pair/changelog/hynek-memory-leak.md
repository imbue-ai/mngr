Fixed unbounded memory growth in `mngr pair`. Unison runs in continuous-sync mode for the whole pairing session and narrates every transfer, and all of that output was retained for the life of the session.

The unison process is now started with `is_output_accumulated=False`, so its output is logged as it arrives and then dropped.
