"""Cooperative cancellation and bounded daemon execution for Query V2."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import queue
import threading
import time
from typing import Any, Literal
from uuid import uuid4

QueryStage = Literal[
    "queued",
    "status",
    "metadata",
    "fts",
    "vector",
    "graph",
    "fallback",
    "context",
    "telemetry",
    "completed",
]


class QueryCancelled(RuntimeError):
    """Raised at a cooperative checkpoint; never claims a thread was killed."""

    def __init__(self, code: str, *, stage: str, worker_state: str = "running") -> None:
        super().__init__(code)
        self.code = code
        self.cancelled_stage = stage
        self.worker_state = worker_state


class QueryCapacityError(RuntimeError):
    def __init__(self, code: str = "query_capacity_exhausted") -> None:
        super().__init__(code)
        self.code = code


@dataclass
class QueryCancellationContext:
    """A monotonic deadline and event shared by every query stage."""

    deadline: float
    cancel_event: threading.Event = field(default_factory=threading.Event)
    stage: QueryStage | str = "queued"
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    on_cancel: Callable[[QueryCancelled], None] | None = field(default=None, repr=False, compare=False)
    _reason: str = field(default="", init=False, repr=False)
    _cancel_notified: bool = field(default=False, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    @classmethod
    def with_timeout(cls, timeout_seconds: float, *, clock: Callable[[], float] = time.monotonic) -> "QueryCancellationContext":
        if timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        return cls(deadline=clock() + timeout_seconds, clock=clock)

    @classmethod
    def unbounded(cls, *, clock: Callable[[], float] = time.monotonic) -> "QueryCancellationContext":
        return cls(deadline=float("inf"), clock=clock)

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def set_stage(self, stage: str) -> None:
        with self._lock:
            self.stage = stage

    def cancel(self, reason: Literal["timeout", "cancelled"] = "cancelled") -> None:
        callback: Callable[[QueryCancelled], None] | None = None
        event: QueryCancelled | None = None
        with self._lock:
            if not self._reason:
                self._reason = reason
            self.cancel_event.set()
            if not self._cancel_notified:
                self._cancel_notified = True
                callback = self.on_cancel
                event = QueryCancelled(
                    "query_timeout" if self._reason == "timeout" else "query_cancelled",
                    stage=str(self.stage),
                )
        if callback is not None and event is not None:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - telemetry must not alter cancellation semantics
                pass

    def set_cancel_handler(self, handler: Callable[[QueryCancelled], None] | None) -> None:
        with self._lock:
            self.on_cancel = handler

    def checkpoint(self, stage: str | None = None) -> float:
        if stage is not None:
            self.set_stage(stage)
        if self.cancel_event.is_set():
            raise QueryCancelled(
                "query_timeout" if self.reason == "timeout" else "query_cancelled",
                stage=str(self.stage),
            )
        remaining = self.remaining
        if remaining <= 0:
            self.cancel("timeout")
            raise QueryCancelled("query_timeout", stage=str(self.stage))
        return remaining

    def checkpoint_batch(self, index: int, *, every: int = 16, stage: str | None = None) -> float | None:
        if index % max(every, 1) != 0:
            return None
        return self.checkpoint(stage)


@dataclass(frozen=True)
class QueryWorkerSnapshot:
    request_id: str
    stage: str
    worker_state: str
    remaining: float


@dataclass
class _Worker:
    request_id: str
    context: QueryCancellationContext
    result_queue: "queue.Queue[tuple[str, Any]]"
    thread: threading.Thread | None = None
    state: str = "queued"


class QueryExecutionRegistry:
    """Bounded daemon workers with honest pending semantics.

    A timeout sets the shared event and waits a finite grace period.  If a
    provider ignores the event, its slot stays occupied until the real thread
    exits; no API claims that Python stopped that thread.
    """

    def __init__(self, max_concurrency: int = 4, *, cancel_grace_seconds: float = 0.25, clock: Callable[[], float] = time.monotonic) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        if cancel_grace_seconds < 0:
            raise ValueError("cancel_grace_seconds must be non-negative")
        self.max_concurrency = max_concurrency
        self.cancel_grace_seconds = cancel_grace_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._workers: dict[str, _Worker] = {}

    def run(
        self,
        function: Callable[[QueryCancellationContext], Any],
        *,
        timeout_seconds: float,
        request_id: str | None = None,
    ) -> Any:
        request_id = request_id or uuid4().hex
        with self._lock:
            if len(self._workers) >= self.max_concurrency:
                return {
                    "ok": False,
                    "code": "query_capacity_exhausted",
                    "worker_state": "capacity_exhausted",
                    "request_id": request_id,
                }
            context = QueryCancellationContext.with_timeout(timeout_seconds, clock=self._clock)
            worker = _Worker(request_id, context, queue.Queue(maxsize=1))
            self._workers[request_id] = worker
            thread = threading.Thread(target=self._worker_main, args=(worker, function), daemon=True, name=f"wiki-query-{request_id[:8]}")
            worker.thread = thread
            thread.start()
        return self._wait(worker)

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            worker = self._workers.get(request_id)
            if worker is None:
                return False
            worker.context.cancel("cancelled")
            return True

    def status(self) -> dict[str, object]:
        with self._lock:
            workers = [
                QueryWorkerSnapshot(item.request_id, str(item.context.stage), item.state, item.context.remaining)
                for item in self._workers.values()
            ]
        return {
            "capacity": self.max_concurrency,
            "active": len(workers),
            "pending": sum(1 for item in workers if item.worker_state == "cancellation_pending"),
            "workers": [
                {
                    "request_id": item.request_id,
                    "stage": item.stage,
                    "worker_state": item.worker_state,
                    "remaining": item.remaining,
                }
                for item in workers
            ],
        }

    def active_count(self) -> int:
        active = self.status()["active"]
        if not isinstance(active, int):
            raise TypeError("status()['active'] must be an int")
        return active

    def _worker_main(self, worker: _Worker, function: Callable[[QueryCancellationContext], Any]) -> None:
        worker.state = "running"
        try:
            value = function(worker.context)
            self._put_result(worker, "ok", value)
            worker.state = "completed"
        except QueryCancelled as exc:
            self._put_result(worker, "cancelled", exc)
            worker.state = "cancelled"
        except BaseException as exc:  # noqa: BLE001 - transport the worker error to caller
            self._put_result(worker, "error", exc)
            worker.state = "failed"
        finally:
            with self._lock:
                self._workers.pop(worker.request_id, None)

    @staticmethod
    def _put_result(worker: _Worker, status: str, value: Any) -> None:
        try:
            worker.result_queue.put_nowait((status, value))
        except queue.Full:
            # The caller already returned a timeout response.  The worker's
            # lifecycle still ends normally and the slot is released in the
            # finally block above.
            pass

    def _wait(self, worker: _Worker) -> Any:
        remaining = worker.context.remaining
        if remaining > 0:
            try:
                status, value = worker.result_queue.get(timeout=remaining)
            except queue.Empty:
                worker.context.cancel("timeout")
                worker.state = "cancellation_pending"
                try:
                    status, value = worker.result_queue.get(timeout=self.cancel_grace_seconds)
                except queue.Empty:
                    return {
                        "ok": False,
                        "code": "query_timeout",
                        "cancelled_stage": str(worker.context.stage),
                        "worker_state": "cancellation_pending",
                        "request_id": worker.request_id,
                    }
                return self._cancelled_result(worker, status, value)
        else:
            worker.context.cancel("timeout")
            worker.state = "cancellation_pending"
            return {
                "ok": False,
                "code": "query_timeout",
                "cancelled_stage": str(worker.context.stage),
                "worker_state": "cancellation_pending",
                "request_id": worker.request_id,
            }
        if status == "ok":
            return value
        if status == "cancelled":
            return self._cancelled_result(worker, status, value)
        if status == "error":
            raise value
        return {"ok": False, "code": "query_failed", "worker_state": worker.state, "request_id": worker.request_id}

    @staticmethod
    def _cancelled_result(worker: _Worker, status: str, value: Any) -> dict[str, object]:
        if isinstance(value, QueryCancelled):
            code = "query_timeout" if worker.context.reason == "timeout" else value.code
            stage = value.cancelled_stage
        else:
            code = "query_timeout" if worker.context.reason == "timeout" else "query_cancelled"
            stage = str(worker.context.stage)
        return {
            "ok": False,
            "code": code,
            "cancelled_stage": stage,
            "worker_state": "cancelled" if status == "cancelled" else worker.state,
            "request_id": worker.request_id,
        }


__all__ = [
    "QueryCancellationContext",
    "QueryCancelled",
    "QueryCapacityError",
    "QueryExecutionRegistry",
    "QueryStage",
]
