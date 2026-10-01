"""Бэкенд Qwen (BazaarLink, OpenAI-совместимый API) на имитации сервера."""

import json
import threading

import httpx
import pytest

from jarvis.llm import LLMError, create_backend
from jarvis.llm.base import ToolCall
from jarvis.llm.qwen_backend import KEY_HINT, ToolCallCollector
from jarvis.tools import TOOLS

TOOL_SCHEMAS = [TOOLS[name].schema() for name in ("open_app", "get_datetime", "set_volume")]


def sse(*chunks) -> bytes:
    lines = []
    for chunk in chunks:
        lines.append(": keep-alive\n\n" if chunk is None else f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def delta(**fields) -> dict:
    return {"choices": [{"index": 0, "delta": fields}]}


class FakeServer:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    def reply(self, status: int = 200, body: bytes | dict = b"") -> None:
        if isinstance(body, dict):
            self.responses.append(httpx.Response(status, json=body))
        else:
            self.responses.append(httpx.Response(status, content=body,
                                                 headers={"content-type": "text/event-stream"}))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def body(self, index: int = -1) -> dict:
        return json.loads(self.requests[index].content)


@pytest.fixture
def server(config, monkeypatch):
    monkeypatch.setenv("BAZAARLINK_API_KEY", "sk-bl-test-key-123456")
    fake = FakeServer()
    backend = create_backend("qwen", config)
    headers = dict(backend.client.headers)
    backend.client = httpx.Client(base_url=backend.base_url, headers=headers, transport=httpx.MockTransport(fake))
    fake.backend = backend
    return fake


def run(backend, messages=None, tools=TOOL_SCHEMAS, allow_tools=True):
    texts = []
    turn = backend.stream_chat("Ты — Джарвис.", messages or [{"role": "user", "content": "Привет"}],
                               tools, allow_tools, threading.Event(), texts.append)
    return turn, texts


def test_missing_key(config, monkeypatch):
    monkeypatch.delenv("BAZAARLINK_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    with pytest.raises(LLMError) as error:
        create_backend("qwen", config)
    assert "BAZAARLINK_API_KEY" in error.value.message and error.value.hint == KEY_HINT


def test_request_format(server):
    server.reply(body=sse(delta(role="assistant", content="Добрый"), None, delta(content=" вечер, сэр.")))
    turn, texts = run(server.backend)
    assert turn.text == "Добрый вечер, сэр." and texts == ["Добрый", " вечер, сэр."]
    request = server.requests[0]
    assert str(request.url) == "https://api.bazaarlink.ai/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-bl-test-key-123456"
    assert request.headers["x-free-fallback"] == "false"  # бесплатный лимит не переходит в платный
    body = server.body()
    assert body["model"] == "qwen/qwen3.7-flash:free" and body["stream"] is True
    assert body["enable_thinking"] is False and body["tool_choice"] == "auto"
    assert body["messages"][0] == {"role": "system", "content": "Ты — Джарвис."}
    assert [t["function"]["name"] for t in body["tools"]] == ["open_app", "get_datetime", "set_volume"]
    assert body["tools"][0]["type"] == "function"


def test_last_step_forbids_tools(server):
    server.reply(body=sse(delta(content="Готово.")))
    turn, _ = run(server.backend, allow_tools=False)
    assert server.body()["tool_choice"] == "none" and turn.tool_calls == []


def test_streamed_tool_calls(server):
    server.reply(body=sse(
        delta(tool_calls=[{"index": 0, "id": "call_a", "type": "function",
                           "function": {"name": "open_app", "arguments": ""}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": '{"name": "бл'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'окнот"}'}}]),
        delta(tool_calls=[{"index": 1, "id": "call_b", "function": {"name": "set_volume", "arguments": '{"level": 40}'}}]),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ))
    turn, texts = run(server.backend)
    assert texts == [] and turn.text == ""
    assert [(c.id, c.name, c.arguments) for c in turn.tool_calls] == [
        ("call_a", "open_app", {"name": "блокнот"}), ("call_b", "set_volume", {"level": 40})]


def test_history_with_tool_results(server):
    server.reply(body=sse(delta(content="Блокнот открыт, сэр.")))
    call = ToolCall(name="open_app", arguments={"name": "блокнот"}, id="call_1")
    history = [
        {"role": "user", "content": "Открой блокнот"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_1", "name": "open_app", "content": "Запущено «блокнот»."},
        {"role": "tool", "tool_call_id": "call_lost", "name": "note", "content": "Заметка сохранена."},
    ]
    run(server.backend, history)
    messages = server.body()["messages"]
    assert messages[2] == {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": "open_app", "arguments": '{"name": "блокнот"}'}}]}
    assert messages[3] == {"role": "tool", "tool_call_id": "call_1", "content": "Запущено «блокнот»."}
    assert messages[4] == {"role": "user", "content": "[Результат note]: Заметка сохранена."}


def test_thinking_is_hidden_and_text_tool_call_rescued(server):
    server.reply(body=sse(delta(content="<think>подумаю"), delta(content="</think>Слушаю, сэр.")))
    turn, texts = run(server.backend)
    assert turn.text == "Слушаю, сэр." and "".join(texts) == "Слушаю, сэр."
    server.reply(body=sse(delta(content='<tool_call>{"name": "get_datetime", "arguments": {}}</tool_call>')))
    turn, texts = run(server.backend)
    assert texts == [] and [c.name for c in turn.tool_calls] == ["get_datetime"]


def test_cancel_stops_reading(server):
    server.reply(body=sse(delta(content="Раз."), delta(content=" Два.")))
    cancel = threading.Event()
    texts = []

    def on_text(text):
        texts.append(text)
        cancel.set()

    turn = server.backend.stream_chat("s", [{"role": "user", "content": "x"}], [], True, cancel, on_text)
    assert texts == ["Раз."] and turn.text == "Раз."


@pytest.mark.parametrize("status, body, expected", [
    (401, {"error": {"message": "Invalid or disabled API key.", "type": "invalid_request_error", "code": 401}},
     "недействителен"),
    (402, {"error": {"message": "Insufficient credits", "code": "insufficient_credits"}}, "не хватает средств"),
    (404, {"error": {"message": "Model not found", "code": "model_not_found"}}, "не найдена"),
    (429, {"error": {"message": "Rate limit", "code": 429}}, "бесплатный лимит"),
    (503, {"error": {"message": "no upstream"}}, "временно недоступен"),
])
def test_http_errors_are_explained(server, status, body, expected):
    server.reply(status, body)
    with pytest.raises(LLMError) as error:
        run(server.backend)
    assert expected in error.value.message
    if status == 401:
        assert error.value.hint == KEY_HINT


def test_error_inside_stream(server):
    server.reply(body=sse(delta(content="Начинаю"), {"error": {"message": "upstream dropped", "code": "upstream_error"}}))
    with pytest.raises(LLMError) as error:
        run(server.backend)
    assert "upstream dropped" in error.value.message


def test_retry_without_thinking_parameter(server):
    server.reply(400, {"error": {"message": "Unrecognized request argument: enable_thinking"}})
    server.reply(body=sse(delta(content="Готово.")))
    turn, _ = run(server.backend)
    assert turn.text == "Готово."
    assert "enable_thinking" in server.body(0) and "enable_thinking" not in server.body(1)


def test_check_validates_key_and_model(server):
    models = {"data": [{"id": "qwen/qwen3.7-flash:free", "aliases": []},
                       {"id": "qwen3.7-flash", "aliases": ["qwen/qwen3.7-flash"]}]}
    server.reply(200, models)
    assert server.backend.check() is None
    assert server.requests[-1].url.path == "/v1/models"
    server.backend.config.set("qwen.model", "qwen/qwen3.7-flash")  # псевдоним тоже подходит
    server.reply(200, models)
    assert server.backend.check() is None
    server.backend.config.set("qwen.model", "qwen/qwen9-ultra")
    server.reply(200, models)
    with pytest.raises(LLMError) as error:
        server.backend.check()
    assert "qwen/qwen9-ultra" in error.value.message and "qwen/qwen3.7-flash:free" in error.value.hint
    server.reply(401, {"error": {"message": "Invalid or disabled API key.", "code": 401}})
    with pytest.raises(LLMError):
        server.backend.check()


def test_connection_error_is_explained(config, monkeypatch):
    monkeypatch.setenv("BAZAARLINK_API_KEY", "sk-bl-x")
    backend = create_backend("qwen", config)

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    backend.client = httpx.Client(base_url=backend.base_url, transport=httpx.MockTransport(refuse))
    with pytest.raises(LLMError) as error:
        run(backend)
    assert "api.bazaarlink.ai" in error.value.message and "прокси" in error.value.hint


def test_collector_handles_provider_quirks():
    collector = ToolCallCollector()
    collector.feed([{"id": "x", "function": {"name": "open_app", "arguments": '{"name": "калькулятор"}'}}])
    collector.feed([{"id": "x", "function": {"name": "open_app", "arguments": '{"name": "калькулятор"}'}}])  # повтор целиком
    collector.feed([{"index": 1, "function": {"name": "mute", "arguments": {"on": True}}}])  # объект вместо строки
    calls = collector.calls()
    assert [(c.name, c.arguments) for c in calls] == [("open_app", {"name": "калькулятор"}), ("mute", {"on": True})]
    assert calls[0].id == "x"


def test_save_secret_writes_env(config, tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# мои ключи\nGEMINI_API_KEY=abc\n", encoding="utf-8")
    config.env_path = env
    monkeypatch.delenv("BAZAARLINK_API_KEY", raising=False)
    config.save_secret("BAZAARLINK_API_KEY", "  sk-bl-new-key  ")
    text = env.read_text(encoding="utf-8")
    assert "# мои ключи" in text and "GEMINI_API_KEY=abc" in text and "sk-bl-new-key" in text
    import os

    assert os.environ["BAZAARLINK_API_KEY"] == "sk-bl-new-key"
    config.save_secret("BAZAARLINK_API_KEY", "sk-bl-second")
    from dotenv import dotenv_values

    assert dotenv_values(env)["BAZAARLINK_API_KEY"] == "sk-bl-second"
    missing = tmp_path / "new" / ".env"
    missing.parent.mkdir()
    config.env_path = missing
    config.save_secret("QWEN_API_KEY", "k")
    assert dotenv_values(missing)["QWEN_API_KEY"] == "k"
