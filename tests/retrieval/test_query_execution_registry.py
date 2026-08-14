from __future__ import annotations

from collections.abc import Callable
import threading
import time

from retrieval.query_cancellation import QueryExecutionRegistry


def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def test_cooperative_timeout_releases_slot_after_checkpoint() -> None:
    registry = QueryExecutionRegistry(max_concurrency=1, cancel_grace_seconds=0.05)
    started = threading.Event()

    def worker(context):
        started.set()
        while True:
            context.checkpoint("vector")
            time.sleep(0.01)

    result = registry.run(worker, timeout_seconds=0.05, request_id="cooperative")

    assert started.is_set()
    assert result["code"] == "query_timeout"
    assert result["cancelled_stage"] == "vector"
    assert result["worker_state"] == "cancelled"
    assert _wait_until(lambda: registry.active_count() == 0)


def test_noncooperative_timeout_reports_pending_and_keeps_slot_until_exit() -> None:
    registry = QueryExecutionRegistry(max_concurrency=1, cancel_grace_seconds=0.01)
    started = threading.Event()
    release = threading.Event()

    def worker(context):
        started.set()
        release.wait(1.0)
        return {"ok": True}

    result = {}
    caller = threading.Thread(
        target=lambda: result.update(registry.run(worker, timeout_seconds=0.03, request_id="pending")),
        daemon=True,
    )
    caller.start()
    assert started.wait(1.0)
    caller.join(1.0)

    assert result["code"] == "query_timeout"
    assert result["worker_state"] == "cancellation_pending"
    assert registry.status()["pending"] == 1
    assert registry.active_count() == 1

    release.set()
    assert _wait_until(lambda: registry.active_count() == 0)


def test_capacity_exhaustion_is_fast_and_does_not_start_an_extra_worker() -> None:
    registry = QueryExecutionRegistry(max_concurrency=1)
    started = threading.Event()
    release = threading.Event()
    first_result = {}

    def worker(context):
        started.set()
        release.wait(1.0)
        return {"ok": True}

    caller = threading.Thread(
        target=lambda: first_result.update(registry.run(worker, timeout_seconds=1.0, request_id="first")),
        daemon=True,
    )
    caller.start()
    assert started.wait(1.0)

    second = registry.run(worker, timeout_seconds=0.5, request_id="second")
    assert second["code"] == "query_capacity_exhausted"
    assert second["worker_state"] == "capacity_exhausted"
    assert registry.active_count() == 1

    release.set()
    caller.join(1.0)
    assert first_result == {"ok": True}
    assert registry.active_count() == 0


def test_explicit_cancel_is_cooperative_and_finish_is_not_reclassified_as_timeout() -> None:
    registry = QueryExecutionRegistry(max_concurrency=1, cancel_grace_seconds=0.2)
    started = threading.Event()

    def worker(context):
        started.set()
        while True:
            context.checkpoint("context")
            time.sleep(0.01)

    result = {}
    caller = threading.Thread(
        target=lambda: result.update(registry.run(worker, timeout_seconds=1.0, request_id="cancelled")),
        daemon=True,
    )
    caller.start()
    assert started.wait(1.0)
    assert registry.cancel("cancelled") is True
    caller.join(1.0)

    assert result["code"] == "query_cancelled"
    assert result["cancelled_stage"] == "context"
    assert registry.active_count() == 0
