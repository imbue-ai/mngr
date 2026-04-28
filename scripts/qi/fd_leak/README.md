# FD Leak Reproduction Scripts

Scripts for reproducing and investigating file descriptor leaks in `list_agents`.

## Fixed issues

Two FD leak sources were identified and fixed:

1. **SSH connection leak**: Host objects created by `get_host()` during discovery and
   detail collection were never disconnected. Fixed by adding `disconnect()` calls in
   `finally` blocks in `ProviderInstanceInterface.get_host_and_agent_details()` and
   `_discover_agents_and_disconnect()`.

2. **Gevent Hub pipe leak**: pyinfra uses gevent greenlets for subprocess I/O. Each
   `ConcurrencyGroupExecutor` thread that uses pyinfra gets its own gevent Hub with
   OS-level pipes. Fixed by destroying the Hub via a global `on_thread_exit` callback
   registered at mngr startup.

## Remaining issue: socket leak when providers discover in parallel

When `discover_hosts_and_agents` runs local + modal providers in parallel (via
`ConcurrencyGroupExecutor`), ~24 sockets leak per call. The leak does NOT occur when:

- Only one provider is active (local-only or modal-only)
- Both providers run sequentially on the main thread

The leak could not be reproduced with just the Modal SDK + threading outside of mngr.
It appears to be specific to the interaction between the mngr provider discovery
pipeline and grpclib's connection management when used from ConcurrencyGroupExecutor
threads.

## Scripts

### `repro_list_agents_fd_leak.py`

High-level regression test. Calls `list_agents` repeatedly and monitors FD count.

```
uv run python scripts/qi/fd_leak/repro_list_agents_fd_leak.py --iterations 10 --interval 0.5
```

### `repro_fd_leak_discover_only.py`

Isolates the discovery phase. Runs `discover_hosts_and_agents` for local-only, modal-only,
and both providers, showing that the leak only occurs when both run in parallel.

```
uv run python scripts/qi/fd_leak/repro_fd_leak_discover_only.py
```

### `repro_grpclib_fd_leak.py`

Compares parallel discovery (leaks) vs sequential discovery (no leak) to demonstrate
that the socket leak is specific to running providers in a ConcurrencyGroupExecutor.

```
uv run python scripts/qi/fd_leak/repro_grpclib_fd_leak.py --iterations 10
```
