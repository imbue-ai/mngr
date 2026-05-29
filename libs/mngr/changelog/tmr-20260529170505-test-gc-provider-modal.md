Fixed the `test_gc_provider_modal` e2e tutorial test so it reliably passes. The
test now creates a Modal command agent before running `mngr gc --provider
modal`, so the Modal provider actually has state to scan and the Modal CLI is
trackably invoked (satisfying the modal/rsync resource guards). It also asserts
the running agent survives garbage collection, and was given an explicit
timeout to accommodate the remote Modal round trip.
