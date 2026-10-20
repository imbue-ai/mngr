A caller's bare `--memory` start arg (`mngr create -s --memory=16g`) is now brought under the no-swap policy: the provider appends a matching `--memory-swap` when the caller sets a memory cap without a swap cap. Before, with `default_memory` configured, the caller's larger memory cap left the defaults' smaller swap cap in place and docker refused to run the container.

Docker discovery now reports each host's recorded size (`DiscoveredHost.size`) from the host records it already reads, so the `mngr observe` stream carries it.
