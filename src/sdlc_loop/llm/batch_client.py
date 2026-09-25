"""Batch API transport for asynchronous golden-eval runs (50% token discount).

The SDLC pipeline is sequential *within* a run but independent *across* runs.
So when the eval harness executes N runs concurrently (one thread each), every
run is blocked on exactly one LLM call at any moment. This client parks each
call, and once every active run is waiting (or ``max_wait_s`` elapses) it
submits all parked calls as one Message Batch, polls until it ends and hands
each result back to its waiting thread. The graph code is unchanged — it just
sees a slow ``complete()``.

Tradeoffs: minutes-to-hours of latency per pipeline stage (fine for evals, never
for interactive runs), per-call latency metrics become meaningless, and cache
hits inside a batch are best-effort.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass

import anthropic
from anthropic.types.messages.batch_create_params import Request

from sdlc_loop.llm.anthropic_client import build_params, response_from_message
from sdlc_loop.llm.types import LLMError, LLMRequest, LLMResponse

log = logging.getLogger(__name__)


@dataclass(slots=True)
class _Pending:
    custom_id: str
    request: LLMRequest
    future: Future[LLMResponse]
    enqueued_at: float


class BatchingLLMClient:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        max_wait_s: float = 20.0,
        poll_interval_s: float = 15.0,
    ) -> None:
        self._client = client or anthropic.Anthropic()
        self._max_wait_s = max_wait_s
        self._poll_interval_s = poll_interval_s
        self._lock = threading.Condition()
        self._pending: list[_Pending] = []
        self._active_workers = 0
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True, name="batch-flusher")
        self._stopped = False
        self._flusher.start()

    @contextmanager
    def worker(self) -> Iterator[None]:
        """Register the calling thread as an active pipeline run."""
        with self._lock:
            self._active_workers += 1
        try:
            yield
        finally:
            with self._lock:
                self._active_workers -= 1
                self._lock.notify_all()

    def complete(self, request: LLMRequest) -> LLMResponse:
        future: Future[LLMResponse] = Future()
        with self._lock:
            self._pending.append(_Pending(uuid.uuid4().hex, request, future, time.monotonic()))
            self._lock.notify_all()
        return future.result()

    def close(self) -> None:
        with self._lock:
            self._stopped = True
            self._lock.notify_all()
        self._flusher.join(timeout=5)

    def _ready(self) -> bool:
        if not self._pending:
            return False
        if len(self._pending) >= max(1, self._active_workers):
            return True
        return time.monotonic() - self._pending[0].enqueued_at >= self._max_wait_s

    def _flush_loop(self) -> None:
        while True:
            with self._lock:
                while not self._stopped and not self._ready():
                    self._lock.wait(timeout=1.0)
                if self._stopped and not self._pending:
                    return
                batch, self._pending = self._pending, []
            if batch:
                self._submit(batch)

    def _submit(self, batch: list[_Pending]) -> None:
        by_id = {p.custom_id: p for p in batch}
        try:
            created = self._client.messages.batches.create(
                requests=[
                    Request(custom_id=p.custom_id, params=build_params(p.request)) for p in batch
                ]
            )
            log.info("batch submitted", extra={"batch_id": created.id, "size": len(batch)})
            started = time.perf_counter()
            while True:
                status = self._client.messages.batches.retrieve(created.id)
                if status.processing_status == "ended":
                    break
                time.sleep(self._poll_interval_s)
            elapsed_ms = (time.perf_counter() - started) * 1000
            for result in self._client.messages.batches.results(created.id):
                pending = by_id.pop(result.custom_id, None)
                if pending is None:
                    continue
                if result.result.type == "succeeded":
                    try:
                        pending.future.set_result(
                            response_from_message(result.result.message, elapsed_ms, batched=True)
                        )
                    except LLMError as exc:
                        pending.future.set_exception(exc)
                else:
                    pending.future.set_exception(
                        LLMError(f"batch item {result.custom_id} {result.result.type}")
                    )
        except anthropic.APIError as exc:
            for pending in by_id.values():
                pending.future.set_exception(LLMError(f"batch submission failed: {exc}"))
            return
        for pending in by_id.values():  # results missing from the batch output
            pending.future.set_exception(LLMError(f"batch item {pending.custom_id} missing"))
