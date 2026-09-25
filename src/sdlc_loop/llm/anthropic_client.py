"""Claude API transport (official ``anthropic`` SDK).

* Structured outputs via ``output_config.format`` — the SDK's public
  ``transform_schema`` turns our Pydantic models into the strict JSON schema the
  API accepts (unsupported constraints such as ``minimum`` are moved into the
  description and re-checked client-side by Pydantic in the agent layer).
* Streaming + ``get_final_message()`` so long Dev outputs never hit HTTP timeouts.
* SDK-level retries handle 408/409/429/5xx and connection errors with backoff;
  anything left is surfaced as :class:`LLMError`.
"""

from __future__ import annotations

import time

import anthropic
from anthropic import transform_schema
from anthropic.types import Message, MessageParam, OutputConfigParam, TextBlockParam
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming

from sdlc_loop.llm.models import get_model_spec
from sdlc_loop.llm.types import LLMError, LLMRefusalError, LLMRequest, LLMResponse, TokenUsage


def build_params(request: LLMRequest) -> MessageCreateParamsNonStreaming:
    """Translate an :class:`LLMRequest` into Messages API parameters.

    Shared by the synchronous client and the Batch API client so both send
    byte-identical prompts (and therefore share cache entries).
    """
    spec = get_model_spec(request.model_id)
    system: list[TextBlockParam] = []
    for block in request.system:
        param: TextBlockParam = {"type": "text", "text": block.text}
        if block.cache:
            param["cache_control"] = {"type": "ephemeral"}
        system.append(param)

    output_config: OutputConfigParam = {
        "format": {"type": "json_schema", "schema": transform_schema(request.output_schema)}
    }
    if request.effort and spec.supports_effort:
        output_config["effort"] = request.effort  # type: ignore[typeddict-item]

    messages: list[MessageParam] = [{"role": "user", "content": request.user_content}]
    params: MessageCreateParamsNonStreaming = {
        "model": request.model_id,
        "max_tokens": request.max_tokens,
        "system": system,
        "messages": messages,
        "output_config": output_config,
    }
    return params


def response_from_message(message: Message, latency_ms: float, *, batched: bool) -> LLMResponse:
    if message.stop_reason == "refusal":
        raise LLMRefusalError(f"model refused (request_id={message.id})")
    text = next((block.text for block in message.content if block.type == "text"), "")
    usage = message.usage
    return LLMResponse(
        text=text,
        usage=TokenUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            cache_read_input_tokens=usage.cache_read_input_tokens or 0,
        ),
        model_id=message.model,
        stop_reason=message.stop_reason,
        latency_ms=round(latency_ms, 1),
        batched=batched,
        request_id=message.id,
    )


class AnthropicLLMClient:
    def __init__(
        self,
        client: anthropic.Anthropic | None = None,
        *,
        max_retries: int = 3,
        timeout_s: float = 600.0,
    ) -> None:
        # Credentials resolve from the environment (ANTHROPIC_API_KEY or an
        # `ant auth login` profile) — never passed through our own config.
        self._client = client or anthropic.Anthropic(max_retries=max_retries, timeout=timeout_s)

    def verify(self, model_id: str) -> str | None:
        """Check credentials with one free ``models.retrieve`` call; return a problem or None."""
        try:
            self._client.models.retrieve(model_id)
        except (anthropic.APIError, TypeError) as exc:  # TypeError: no credential source
            return _credential_problem(exc, model_id)
        return None

    def complete(self, request: LLMRequest) -> LLMResponse:
        params = build_params(request)
        started = time.perf_counter()
        try:
            with self._client.messages.stream(
                model=params["model"],
                max_tokens=params["max_tokens"],
                system=params["system"],
                messages=params["messages"],
                output_config=params["output_config"],
            ) as stream:
                message = stream.get_final_message()
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Claude API connection error: {exc}") from exc
        return response_from_message(message, (time.perf_counter() - started) * 1000, batched=False)


def _credential_problem(exc: Exception, model_id: str) -> str:
    known: tuple[tuple[type[Exception], str], ...] = (
        (anthropic.AuthenticationError, "the API key was rejected (401)"),
        (anthropic.PermissionDeniedError, "the API key lacks permission for this API (403)"),
        (anthropic.NotFoundError, f"model {model_id} is not available to this key (404)"),
        (anthropic.APIConnectionError, "api.anthropic.com could not be reached"),
        (TypeError, "no usable Anthropic credentials were found"),
    )
    for kind, message in known:
        if isinstance(exc, kind):
            return message
    code = getattr(exc, "status_code", "error")
    return f"the API returned {code}"
