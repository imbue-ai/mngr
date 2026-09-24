import gc
import weakref
from threading import Thread

from imbue.concurrency_group.event_utils import ShutdownEvent
from imbue.concurrency_group.thread_utils import ObservableThread


def test_shutdown_event_can_be_waited_from_multiple_threads() -> None:
    shutdown_event = ShutdownEvent.build_root()
    child = ShutdownEvent.from_parent(shutdown_event)

    threads = [ObservableThread(target=lambda: child.wait()) for _ in range(4)]
    for thread in threads:
        thread.start()
    shutdown_event.set()
    for thread in threads:
        thread.join()
        thread.maybe_raise()


def test_shutdown_event_wait_returns_false_on_timeout() -> None:
    """Test that wait() returns False when the timeout expires without the event being set."""
    shutdown_event = ShutdownEvent.build_root()
    result = shutdown_event.wait(timeout=0.02)
    assert result is False


def test_shutdown_event_wait_returns_true_when_parent_event_is_set() -> None:
    """Test that wait() returns True when the parent event is set."""
    parent = ShutdownEvent.build_root()
    child = ShutdownEvent.from_parent(parent)

    # Set the parent event in another thread after a short delay
    def set_parent() -> None:
        parent.set()

    thread = Thread(target=set_parent)
    thread.start()
    result = child.wait(timeout=1.0)
    thread.join()
    assert result is True


def test_shutdown_event_is_set_via_parent_event() -> None:
    """Test that is_set() returns True when the parent event is set."""
    parent = ShutdownEvent.build_root()
    child = ShutdownEvent.from_parent(parent)

    assert child.is_set() is False
    parent.set()
    assert child.is_set() is True


def test_shutdown_event_is_set_via_own_event() -> None:
    """Test that is_set() returns True when the own event is set."""
    shutdown_event = ShutdownEvent.build_root()

    assert shutdown_event.is_set() is False
    shutdown_event.set()
    assert shutdown_event.is_set() is True


def test_shutdown_event_created_from_an_already_set_parent_starts_set() -> None:
    parent = ShutdownEvent.build_root()
    parent.set()

    child = ShutdownEvent.from_parent(parent)

    assert child.is_set() is True
    assert child.wait(timeout=0) is True


def test_setting_a_shutdown_event_sets_its_descendants_but_not_its_parent() -> None:
    root = ShutdownEvent.build_root()
    middle = ShutdownEvent.from_parent(root)
    leaf = ShutdownEvent.from_parent(middle)

    middle.set()

    assert leaf.is_set() is True
    assert middle.is_set() is True
    assert root.is_set() is False


def test_call_on_set_runs_the_callback_once_when_an_ancestor_is_set() -> None:
    root = ShutdownEvent.build_root()
    child = ShutdownEvent.from_parent(root)
    calls: list[str] = []

    with child.call_on_set(lambda: calls.append("called")):
        assert calls == []
        root.set()
        root.set()
        child.set()

    assert calls == ["called"]


def test_call_on_set_runs_the_callback_immediately_when_already_set() -> None:
    shutdown_event = ShutdownEvent.build_root()
    shutdown_event.set()
    calls: list[str] = []

    with shutdown_event.call_on_set(lambda: calls.append("called")):
        assert calls == ["called"]


def test_call_on_set_does_not_run_the_callback_after_its_block_exits() -> None:
    root = ShutdownEvent.build_root()
    child = ShutdownEvent.from_parent(root)
    calls: list[str] = []

    with child.call_on_set(lambda: calls.append("called")):
        pass
    root.set()

    assert calls == []


def test_shutdown_event_does_not_keep_discarded_children_alive() -> None:
    # A long-lived group's event gains a child per process it ever runs, so
    # holding them strongly would grow without bound.
    root = ShutdownEvent.build_root()
    child_reference = weakref.ref(ShutdownEvent.from_parent(root))

    gc.collect()

    assert child_reference() is None
    root.set()
    assert root.is_set() is True


def test_shutdown_event_reaches_a_descendant_whose_intermediate_ancestor_was_discarded() -> None:
    root = ShutdownEvent.build_root()
    leaf = ShutdownEvent.from_parent(ShutdownEvent.from_parent(root))

    gc.collect()
    root.set()

    assert leaf.is_set() is True
