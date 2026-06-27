"""Async OpenAI wrapper for structured-output adjudication.

Thin layer over `AsyncOpenAI` returning validated pydantic objects. Retries transient failures;
caps in-flight requests with a semaphore for batch calls. The model defaults to `gpt-5.4-mini`
(configurable). The cache (layer 2) is consulted by the pipeline *before* calling here, so this
layer is pure I/O.
"""

from __future__ import annotations

import asyncio
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from tenacity import retry, stop_after_attempt, wait_exponential

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
        temperature: float = 0.0,
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
               reraise=True)
        async def _call() -> str:
            resp = await self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format=response_format(schema_model, schema_name),
            )
            return resp.choices[0].message.content or ""

        async with self._semaphore:
            try:
                content = await _call()
                return schema_model.model_validate_json(content)
            except (ValidationError, Exception) as exc:  # noqa: BLE001 — normalize to LLMError
                if isinstance(exc, LLMError):
                    raise
                raise LLMError(f"{schema_name} call failed: {exc}") from exc

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
        if close is not None:
            result = close()
            if asyncio.iscoroutine(result):
                await result
