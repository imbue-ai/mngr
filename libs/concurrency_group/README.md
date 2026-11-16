# Concurrency Group

A library for managing threads and processes in a structured way. It provides the `ConcurrencyGroup` class which helps avoid accidentally leaking threads and processes, and helps shut them down gracefully.

## Key Features

- Track threads and processes created within a context manager
- Ensure proper cleanup and failure handling
- Support nested concurrency groups
- Propagate shutdown events to all threads and processes
- Detect and report timeouts and failures

## Basic Usage

```python
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup

with ConcurrencyGroup(name="main") as cg:
    # Start a thread
    thread = cg.start_new_thread(target=my_function)

    # Run a process in the background
    process = cg.run_process_in_background(["echo", "hello"])

    # Run a process to completion
    result = cg.run_process_to_completion(["ls", "-la"])

# All threads and processes are automatically waited for on exit
```

## Nested Groups

```python
with ConcurrencyGroup(name="outer") as outer_cg:
    with outer_cg.make_concurrency_group(name="inner") as inner_cg:
        inner_cg.start_new_thread(target=my_function)
```

## Process Cleanup

By default each process runs in a session of its own, and stopping one signals its whole
process group -- so a process that spawned work of its own does not leave that work
running, and whatever it left behind when it exited is swept as well. The group does not
return from its context manager until the processes it had to stop have actually died:
they are sent SIGTERM, given a few seconds, and then SIGKILLed. Stopping reaches
everything still in the process's group; a daemon that starts a session of its own, such
as a tmux server, is left alone.

The one thing a process gives up for this is the controlling terminal. A command that
must prompt the user there (`sudo` asking for a password, say) is run with
`is_own_process_group=False`, which keeps it in the caller's process group; stopping it
then reaches only that one process.

## Shutdown Support

```python
with ConcurrencyGroup(name="main") as cg:
    thread = cg.start_new_thread(target=my_function)
    # Trigger shutdown of all strands
    cg.shutdown()
```
