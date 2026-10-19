"""Async OpenAI wrapper for structured-output adjudication.

Thin layer over `AsyncOpenAI` returning validated pydantic objects. Retries transient failures;
caps in-flight requests with a semaphore for batch calls. The model defaults to `gpt-6-luna`
(configurable). `temperature` is sent only when set; otherwise the model default applies. The cache (layer 2) is consulted by the pipeline *before* calling here, so this
layer is pure I/O.

A refusal, or an answer the API cut off (token limit, content filter), raises `LLMError` naming that
reason. It is not retried, because retrying would ask the same question again, and it is never
passed on as a parse failure of half a JSON document.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from reference_audit.llm.schemas import response_format

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """A structured-output call failed after retries (caller leaves the entry unresolved)."""


class LLMClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str | None = None,
        concurrency: int = 8,
        temperature: float | None = None,
        client=None,
    ):
        self.model = model
        self.temperature = temperature
        self._semaphore = asyncio.Semaphore(concurrency)
        if client is not None:
            self._client = client
        else:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    @property
    def available(self) -> bool:
        return self._client is not None

    async def structured(
        self, system: str, user: str, schema_model: type[T], schema_name: str
    ) -> T:
        """One structured call → a validated `schema_model` instance. Raises LLMError on failure."""

        @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=0.5, min=0.5, max=8),
               retry=retry_if_not_exception_type(LLMError), reraise=True)
        async def _call() -> str:
            extra = {} if self.temperature is None else {"temperature": self.temperature}
            resp = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_format(schema_model, schema_name),
                **extra,
            )
            choice = resp.choices[0]
            refusal = getattr(choice.message, "refusal", None)
            if refusal:
                raise LLMError(f"{schema_name}: the model refused: {refusal}")
            finish = getattr(choice, "finish_reason", None)
            if finish in ("length", "content_filter"):
                cause = ("the output token limit" if finish == "length"
                         else "the content filter")
                raise LLMError(f"{schema_name}: the answer was cut off by {cause}")
            return choice.message.content or ""

        async with self._semaphore:
            try:
                content = await _call()
                return schema_model.model_validate_json(content)
            except (ValidationError, Exception) as exc:  # noqa: BLE001 — normalize to LLMError
                if isinstance(exc, LLMError):
                    raise
                raise LLMError(f"{schema_name} call failed: {exc}") from exc

    async def read_pdf(
        self, pdf_path: str | Path, *, model: str, instructions: str, prompt: str,
        schema_model: type[T], timeout: float = 1800.0,
    ) -> tuple[T, dict]:
        """Give the model a PDF (its text layer and page images) and return its structured answer,
        validated, with the token usage. Raises LLMError naming why there is no answer.

        Sent through the Responses API, which accepts a PDF as an `input_file`. `model` overrides the
        client's adjudication model, because extraction and its escalation use their own models. The
        SDK retries transient transport errors; a refusal, a cut-off answer or an unparseable one is
        not retried here, since asking again would cost another full read of the document.
        """
        path = Path(pdf_path)
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        async with self._semaphore:
            try:
                resp = await self._client.responses.parse(
                    model=model,
                    instructions=instructions,
                    input=[{"role": "user", "content": [
                        {"type": "input_file", "filename": path.name,
                         "file_data": f"data:application/pdf;base64,{data}"},
                        {"type": "input_text", "text": prompt},
                    ]}],
                    text_format=schema_model,
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001 — normalize transport/API errors to LLMError
                raise LLMError(f"{model} could not read {path.name}: {type(exc).__name__}: {exc}") \
                    from exc
        for item in resp.output or []:
            for part in getattr(item, "content", None) or []:
                if getattr(part, "type", "") == "refusal":
                    raise LLMError(f"{model} refused to read {path.name}: {part.refusal}")
        if resp.status == "incomplete":
            raise LLMError(f"{model}'s reading of {path.name} was cut off: {resp.incomplete_details}")
        if resp.output_parsed is None:
            raise LLMError(f"{model} returned no parseable answer for {path.name} "
                           f"(status {resp.status})")
        usage = resp.usage.model_dump() if getattr(resp, "usage", None) is not None else {}
        return resp.output_parsed, usage

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
