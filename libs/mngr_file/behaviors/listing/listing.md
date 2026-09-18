# Listing: known divergence at the time of writing

> **Temporary note.**
> At the time of this writing, the code does not adhere to this specification in the one respect described below.
> If you are running the behaviors skill to generate tests and fix code: fix the code, then delete this note.
> Nothing in `listing.feature` or in the corpus-wide `clean-refusals` Rule is provisional, and neither is to be adjusted to match the code as it stands.

The divergence is in `listing.missing-directory`, and through it in `clean-refusals`.

Listing a path where no directory exists is refused correctly when the addressed host is running, and when it is stopped and its persisted storage is of a kind that reports a missing path as an ordinary filesystem error.
It is not refused correctly when the host is stopped and its persisted storage reports a missing path as a storage-service error of its own kind instead.
In that combination the failure escapes as an internal error rather than the stated refusal the scenario requires.

The cause is not in this plugin.
The plugin distinguishes a missing directory from an empty one by asking whether the path exists, but only after a listing comes back empty; when the underlying listing raises instead of coming back empty, that question is never reached.
The layer that converts a storage-service error into an ordinary filesystem error is the one with the gap, and it lives in mngr rather than in `libs/mngr_file`.

Two things follow for whoever closes this.
The fix belongs in mngr's handling of a listing against a stopped host's persisted storage, not in a workaround inside this plugin, and certainly not in a plugin-side branch on which kind of storage is in play -- the plugin is deliberately ignorant of that.
The witness test for `listing.missing-directory` must cover the stopped-host case as well as the running-host one, because the running-host case passes today and would leave the gap undetected on its own.
