"""The micro-batching client coalesces one call per concurrent run into one Batch."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any, cast

import anthropic
from anthropic.types import Message, TextBlock, Usage

from sdlc_loop.llm.batch_client import BatchingLLMClient
from sdlc_loop.llm.types import CallTags, LLMRequest, LLMResponse, SystemBlock
from sdlc_loop.schemas import Persona, ProblemBrief


def _message(model: str, text: str) -> Message:
    return Message.model_construct(
        id="msg_1",
        type="message",
        role="assistant",
        model=model,
        content=[TextBlock.model_construct(type="text", text=text, citations=None)],
        stop_reason="end_turn",
        stop_sequence=None,
        usage=Usage.model_construct(
            input_tokens=100,
            output_tokens=10,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        ),
    )


class FakeBatches:
    def __init__(self) -> None:
        self.created: list[list[Any]] = []

    def create(self, requests: list[Any]) -> SimpleNamespace:
        self.created.append(list(requests))
        return SimpleNamespace(id=str(len(self.created) - 1))

    def retrieve(self, batch_id: str) -> SimpleNamespace:
        return SimpleNamespace(processing_status="ended")

    def results(self, batch_id: str) -> Iterator[SimpleNamespace]:
        for req in self.created[int(batch_id)]:
            text = req["params"]["messages"][0]["content"]
            yield SimpleNamespace(
                custom_id=req["custom_id"],
                result=SimpleNamespace(
                    type="succeeded", message=_message(req["params"]["model"], text)
                ),
            )


def _request(i: int) -> LLMRequest:
    return LLMRequest(
        model_id="claude-haiku-4-5-20251001",
        system=(SystemBlock("sys", cache=True),),
        user_content=f"run-{i}",
        output_schema=ProblemBrief,
        max_tokens=100,
        tags=CallTags(run_id=f"r{i}", persona=Persona.PM, purpose="persona:pm"),
    )


def test_concurrent_runs_are_coalesced_into_one_batch() -> None:
    batches = FakeBatches()
    fake = SimpleNamespace(messages=SimpleNamespace(batches=batches))
    client = BatchingLLMClient(cast(anthropic.Anthropic, fake), max_wait_s=30, poll_interval_s=0)
    results: dict[int, LLMResponse] = {}
    ready = threading.Barrier(3)

    def worker(i: int) -> None:
        with client.worker():
            ready.wait()
            results[i] = client.complete(_request(i))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    client.close()

    assert len(batches.created) == 1 and len(batches.created[0]) == 3
    assert {r.text for r in results.values()} == {"run-0", "run-1", "run-2"}  # routed by custom_id
    assert all(r.batched for r in results.values())
