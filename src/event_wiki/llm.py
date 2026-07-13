from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

from openai import BadRequestError, OpenAI
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
        max_retries: int = 5,
        client: OpenAI | None = None,
    ) -> None:
        self.model_version = model
        self._json_schema_available = True
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    def invoke(
        self,
        *,
        task: str,
        prompt: str,
        output_model: type[OutputT],
        context: dict[str, Any],
    ) -> OutputT:
        schema = output_model.model_json_schema()
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False, default=str),
            },
        ]
        fallback_prompt = (
            f"{prompt}\n\nReturn one JSON object matching this JSON Schema exactly:\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        if not self._json_schema_available:
            response = self._client.chat.completions.create(
                model=self.model_version,
                messages=[{"role": "system", "content": fallback_prompt}, messages[1]],
                response_format={"type": "json_object"},
            )
        else:
            try:
                response = self._client.chat.completions.create(
                    model=self.model_version,
                    messages=messages,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": task,
                            "strict": True,
                            "schema": schema,
                        },
                    },
                )
            except BadRequestError as exc:
                detail = str(exc).lower()
                if "response_format" not in detail or "unavailable" not in detail:
                    raise
                self._json_schema_available = False
                response = self._client.chat.completions.create(
                    model=self.model_version,
                    messages=[
                        {"role": "system", "content": fallback_prompt},
                        messages[1],
                    ],
                    response_format={"type": "json_object"},
                )
        content = response.choices[0].message.content
        if not content:
            raise ValueError(f"{task} returned an empty structured response")
        return output_model.model_validate_json(content)
