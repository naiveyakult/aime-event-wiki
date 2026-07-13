from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

from openai import OpenAI
from pydantic import BaseModel

OutputT = TypeVar("OutputT", bound=BaseModel)


class StructuredLLM(Protocol):
    """Small injectable boundary used by all event-wiki agents."""

    model_version: str

    def invoke(
        self,
        *,
        task: str,
        prompt: str,
        output_model: type[OutputT],
        context: dict[str, Any],
    ) -> OutputT: ...


class OpenAICompatibleStructuredClient:
    """Structured JSON client for OpenAI and OpenAI-compatible chat endpoints."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str | None = None,
        timeout: float = 60.0,
        client: OpenAI | None = None,
    ) -> None:
        self.model_version = model
        self._client = client or OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def invoke(
        self,
        *,
        task: str,
        prompt: str,
        output_model: type[OutputT],
        context: dict[str, Any],
    ) -> OutputT:
        schema = output_model.model_json_schema()
        response = self._client.chat.completions.create(
            model=self.model_version,
            messages=[
                {"role": "system", "content": prompt},
                {
                    "role": "user",
                    "content": json.dumps(context, ensure_ascii=False, default=str),
                },
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": task,
                    "strict": True,
                    "schema": schema,
                },
            },
        )
        content = response.choices[0].message.content
        if not content:
            raise ValueError(f"{task} returned an empty structured response")
        return output_model.model_validate_json(content)
