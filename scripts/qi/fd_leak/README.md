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

## Remaining issue: grpclib socket leak

When multiple providers run in parallel threads (e.g. local + modal), grpclib creates
per-thread gRPC connections that are never closed. This is because `grpclib.client.Channel`
binds its protocol to one asyncio event loop, but Modal's `synchronize_api` creates a
new loop per thread.

- Running providers **sequentially**: no leak (one-time connection setup, then stable)
- Running providers **in parallel**: ~24 sockets leaked per `list_agents` call

This needs a fix in grpclib or the Modal SDK.

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

Minimal reproduction of the grpclib leak using only the Modal SDK and threading. Demonstrates
that Modal API calls from a single thread don't leak, but leak when a parallel thread is active.

```
uv run python scripts/qi/fd_leak/repro_grpclib_fd_leak.py --iterations 10
```
