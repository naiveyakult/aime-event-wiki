from types import SimpleNamespace

import httpx
from openai import BadRequestError
from pydantic import BaseModel

from event_wiki.llm import OpenAICompatibleStructuredClient


class Result(BaseModel):
    value: str


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            request = httpx.Request("POST", "https://api.example.test/chat/completions")
            response = httpx.Response(400, request=request)
            raise BadRequestError(
                "This response_format type is unavailable now",
                response=response,
                body={"error": {"message": "This response_format type is unavailable now"}},
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"value":"ok"}'))]
        )


def test_structured_client_falls_back_to_json_object_when_json_schema_is_unavailable() -> None:
    completions = FakeCompletions()
    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    client = OpenAICompatibleStructuredClient(
        api_key="synthetic-key",
        model="synthetic-model",
        client=fake_client,
    )

    result = client.invoke(
        task="synthetic_task",
        prompt="Return a structured result.",
        output_model=Result,
        context={"input": "synthetic"},
    )

    assert result == Result(value="ok")
    assert completions.calls[0]["response_format"]["type"] == "json_schema"
    assert completions.calls[1]["response_format"] == {"type": "json_object"}
    assert "JSON Schema" in completions.calls[1]["messages"][0]["content"]

    client.invoke(
        task="second_task",
        prompt="Return another structured result.",
        output_model=Result,
        context={"input": "synthetic"},
    )
    assert len(completions.calls) == 3
    assert completions.calls[2]["response_format"] == {"type": "json_object"}


def test_structured_client_configures_bounded_transport_retries(monkeypatch) -> None:
    captured = {}

    def fake_openai(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("event_wiki.llm.OpenAI", fake_openai)

    OpenAICompatibleStructuredClient(
        api_key="synthetic-key",
        model="synthetic-model",
        timeout=120,
        max_retries=5,
    )

    assert captured["timeout"] == 120
    assert captured["max_retries"] == 5
