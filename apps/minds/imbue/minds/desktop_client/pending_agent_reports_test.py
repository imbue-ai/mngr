import threading

from imbue.minds.desktop_client.pending_agent_reports import PendingAgentReportStore


def test_add_returns_id_and_preserves_order() -> None:
    store = PendingAgentReportStore()
    first_id = store.add(description="first broke", workspace_agent_id="agent-1")
    second_id = store.add(description="second broke", workspace_agent_id="agent-2")

    assert first_id != second_id
    pending = store.list_pending()
    assert [report.report_id for report in pending] == [first_id, second_id]
    # ``head`` is the oldest -- the next one a human reviews.
    head = store.head()
    assert head is not None
    assert head.report_id == first_id
    assert head.description == "first broke"
    assert head.workspace_agent_id == "agent-1"


def test_remove_drops_one_report_and_advances_head() -> None:
    store = PendingAgentReportStore()
    first_id = store.add(description="first", workspace_agent_id=None)
    second_id = store.add(description="second", workspace_agent_id=None)

    assert store.remove(first_id) is True
    # The removed report is gone; the next one becomes the head.
    assert [report.report_id for report in store.list_pending()] == [second_id]
    head = store.head()
    assert head is not None
    assert head.report_id == second_id

    assert store.remove(second_id) is True
    assert store.head() is None
    assert store.list_pending() == []


def test_remove_unknown_id_is_a_noop() -> None:
    store = PendingAgentReportStore()
    kept_id = store.add(description="keep me", workspace_agent_id=None)

    # Idempotent: removing an already-removed / never-present id changes nothing (the submit and
    # discard paths can both try to remove the same id).
    assert store.remove("does-not-exist") is False
    assert store.remove(kept_id) is True
    assert store.remove(kept_id) is False
    assert store.head() is None


def test_add_and_remove_wake_every_subscriber() -> None:
    store = PendingAgentReportStore()
    first_event = threading.Event()
    second_event = threading.Event()
    store.subscribe(first_event)
    store.subscribe(second_event)

    report_id = store.add(description="wake up", workspace_agent_id=None)
    assert first_event.is_set()
    assert second_event.is_set()

    first_event.clear()
    second_event.clear()
    store.remove(report_id)
    assert first_event.is_set()
    assert second_event.is_set()


def test_unsubscribed_connection_is_not_woken() -> None:
    store = PendingAgentReportStore()
    wake_event = threading.Event()
    store.subscribe(wake_event)
    store.unsubscribe(wake_event)

    store.add(description="nobody listening", workspace_agent_id=None)
    assert not wake_event.is_set()


def test_concurrent_adds_all_retained() -> None:
    """Every concurrently-added report survives -- the whole point of the durable queue is that a report
    is never dropped just because another arrives at the same time."""
    store = PendingAgentReportStore()
    report_count = 50
    barrier = threading.Barrier(report_count)

    def _add(index: int) -> None:
        # Release all threads together so the adds genuinely contend on the store's lock.
        barrier.wait()
        store.add(description=f"report {index}", workspace_agent_id=None)

    threads = [threading.Thread(target=_add, args=(index,)) for index in range(report_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    pending = store.list_pending()
    assert len(pending) == report_count
    # No duplicates or lost ids.
    assert len({report.report_id for report in pending}) == report_count
    assert {report.description for report in pending} == {f"report {index}" for index in range(report_count)}
