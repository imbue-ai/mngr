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

## Shutdown Support

```python
with ConcurrencyGroup(name="main") as cg:
    thread = cg.start_new_thread(target=my_function)
    # Trigger shutdown of all strands
    cg.shutdown()
```
