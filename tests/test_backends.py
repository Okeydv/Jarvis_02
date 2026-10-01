"""Проверка перевода истории в форматы API и разбора потоков — без сети, на классах самих библиотек."""

import json
import ssl
import threading

import pytest

from jarvis.llm import LLMError, create_backend
from jarvis.llm.base import ToolCall
from jarvis.llm.ollama_backend import ThinkFilter
from jarvis.tools import TOOLS

TOOL_SCHEMAS = [t.schema() for t in TOOLS.values()]


def history_with_parallel_calls(backend_name=None, raw=None):
    calls = [ToolCall(name="set_volume", arguments={"level": 30}, id="c1"),
             ToolCall(name="get_datetime", arguments={}, id="c2")]
    return [
        {"role": "user", "content": "Сделай тише и скажи время"},
        {"role": "assistant", "content": "", "tool_calls": calls,
         "raw": {"backend": backend_name, "data": raw} if backend_name else None},
        {"role": "tool", "tool_call_id": "c1", "name": "set_volume", "content": "Громкость установлена на 30%."},
        {"role": "tool", "tool_call_id": "c2", "name": "get_datetime", "content": "Сейчас 12:00."},
    ]


# ─── Ollama ────────────────────────────────────────────────────────────

def test_think_filter_streaming():
    f = ThinkFilter()
    out = "".join(f.feed(p) for p in ["<thi", "nk>размышляю</th", "ink>\n\nГотово, ", "сэр."]) + f.flush()
    assert out == "Готово, сэр."
    f = ThinkFilter()
    assert f.feed("Обычный <b>текст</b>") + f.flush() == "Обычный <b>текст</b>"


def test_ollama_messages_and_stream(config):
    import ollama

    backend = create_backend("ollama", config)
    messages = backend.build_messages("SYS", history_with_parallel_calls())
    assert messages[0] == {"role": "system", "content": "SYS"}
    assert messages[2]["tool_calls"][0]["function"] == {"name": "set_volume", "arguments": {"level": 30}}
    assert messages[3] == {"role": "tool", "content": "Громкость установлена на 30%.", "tool_name": "set_volume"}
    tools = backend.build_tools(TOOL_SCHEMAS)
    assert tools[0]["type"] == "function" and tools[0]["function"]["name"] == "open_app"

    captured = {}

    def fake_chat(**kwargs):
        captured.clear()
        captured.update(kwargs)
        call = ollama.Message.ToolCall(function=ollama.Message.ToolCall.Function(name="open_app", arguments={"name": "блокнот"}))
        return iter([
            ollama.ChatResponse(model="m", message=ollama.Message(role="assistant", content="<think></think>Откры")),
            ollama.ChatResponse(model="m", message=ollama.Message(role="assistant", content="ваю.")),
            ollama.ChatResponse(model="m", message=ollama.Message(role="assistant", content="", tool_calls=[call]), done=True),
        ])

    backend.client.chat = fake_chat
    pieces = []
    turn = backend.stream_chat("SYS", [{"role": "user", "content": "Открой блокнот"}], TOOL_SCHEMAS, True,
                               threading.Event(), pieces.append)
    assert "".join(pieces) == "Открываю."
    assert turn.tool_calls[0].name == "open_app" and turn.tool_calls[0].arguments == {"name": "блокнот"}
    assert captured["think"] is False and captured["stream"] is True
    assert captured["model"] == "qwen3:8b" and captured["options"]["num_ctx"] == 8192

    turn = backend.stream_chat("SYS", [{"role": "user", "content": "x"}], TOOL_SCHEMAS, False, threading.Event(), lambda d: None)
    assert "tools" not in captured and turn.tool_calls == []


def test_ollama_errors(config):
    import ollama

    backend = create_backend("ollama", config)

    def not_running(**kwargs):
        raise ConnectionError("Failed to connect to Ollama")

    backend.client.chat = not_running
    with pytest.raises(LLMError) as err:
        backend.stream_chat("S", [{"role": "user", "content": "x"}], [], True, threading.Event(), lambda d: None)
    assert "ollama serve" in err.value.hint and "ollama pull qwen3:8b" in err.value.hint

    def missing_model(**kwargs):
        raise ollama.ResponseError("model 'qwen3:8b' not found", 404)

    backend.client.chat = missing_model
    with pytest.raises(LLMError) as err:
        backend.stream_chat("S", [{"role": "user", "content": "x"}], [], True, threading.Event(), lambda d: None)
    assert err.value.hint == "Скачайте её командой: ollama pull qwen3:8b"


# ─── GigaChat ──────────────────────────────────────────────────────────

def test_gigachat_requires_key(config, monkeypatch):
    monkeypatch.delenv("GIGACHAT_CREDENTIALS", raising=False)
    with pytest.raises(LLMError) as err:
        create_backend("gigachat", config)
    assert "GIGACHAT_CREDENTIALS" in err.value.hint


def test_gigachat_messages_and_stream(config, monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "test-key")
    from gigachat.models import ChatCompletionChunk, ChoicesChunk, FunctionCall, MessagesChunk

    backend = create_backend("gigachat", config)
    messages = backend.build_messages("SYS", history_with_parallel_calls("gigachat", {"functions_state_id": "st-1"}))
    roles = [m.role for m in messages]
    # параллельные вызовы разложены на пары «вызов → результат»
    assert roles == ["system", "user", "assistant", "function", "assistant", "function"]
    assert messages[2].function_call.name == "set_volume" and messages[2].functions_state_id == "st-1"
    assert json.loads(messages[3].content) == {"result": "Громкость установлена на 30%."}
    functions = backend.build_functions(TOOL_SCHEMAS)
    media = next(f for f in functions if f.name == "media")
    assert media.parameters.properties["action"].enum == ["play_pause", "next", "previous"]

    sent = {}

    def fake_stream(chat):
        sent["chat"] = chat
        yield ChatCompletionChunk(choices=[ChoicesChunk(delta=MessagesChunk(content="Сейчас "), index=0)],
                                  created=0, model="GigaChat-2", object="chat.completion")
        yield ChatCompletionChunk(choices=[ChoicesChunk(
            delta=MessagesChunk(function_call=FunctionCall(name="get_datetime", arguments={}), functions_state_id="st-2"),
            index=0, finish_reason="function_call")], created=0, model="GigaChat-2", object="chat.completion")

    backend.client.stream = fake_stream
    pieces = []
    turn = backend.stream_chat("SYS", [{"role": "user", "content": "Который час?"}], TOOL_SCHEMAS, True,
                               threading.Event(), pieces.append)
    assert pieces == ["Сейчас "]
    assert turn.tool_calls[0].name == "get_datetime"
    assert turn.raw == {"backend": "gigachat", "data": {"functions_state_id": "st-2"}}
    assert sent["chat"].function_call == "auto" and sent["chat"].model == "GigaChat-2"

    backend.stream_chat("SYS", [{"role": "user", "content": "x"}], TOOL_SCHEMAS, False, threading.Event(), lambda d: None)
    assert sent["chat"].function_call == "none"


def test_gigachat_certificate_error_hint(config, monkeypatch):
    import httpx

    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "test-key")
    backend = create_backend("gigachat", config)

    def broken(chat):
        try:
            raise ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate")
        except ssl.SSLError as exc:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]") from exc
        yield  # pragma: no cover

    backend.client.stream = broken
    with pytest.raises(LLMError) as err:
        backend.stream_chat("S", [{"role": "user", "content": "x"}], [], True, threading.Event(), lambda d: None)
    assert "сертификат" in err.value.message.lower() and "download_models.py --cert" in err.value.hint


def test_gigachat_ssl_context_accepts_der_and_pem(tmp_path):
    from jarvis.llm.gigachat_backend import build_ssl_context

    der = ssl.PEM_cert_to_DER_cert(_SELF_SIGNED_PEM)
    (tmp_path / "ca.cer").write_bytes(der)
    (tmp_path / "ca.crt").write_text(_SELF_SIGNED_PEM)
    for name in ("ca.cer", "ca.crt"):
        context = build_ssl_context(str(tmp_path / name))
        subjects = [c.get("subject") for c in context.get_ca_certs()]
        assert any(("commonName", "Jarvis Test CA") in s[0] for s in subjects if s)
    build_ssl_context(str(tmp_path / "missing.crt"))  # отсутствующий файл — не ошибка


# ─── Gemini ────────────────────────────────────────────────────────────

def test_gemini_requires_key(config, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError) as err:
        create_backend("gemini", config)
    assert "aistudio.google.com" in err.value.hint


def test_gemini_contents_config_and_stream(config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from google.genai import types

    backend = create_backend("gemini", config)
    raw_content = types.Content(role="model", parts=[
        types.Part(function_call=types.FunctionCall(name="set_volume", args={"level": 30}, id="c1"), thought_signature=b"sig"),
        types.Part(function_call=types.FunctionCall(name="get_datetime", args={}, id="c2")),
    ])
    history = history_with_parallel_calls("gemini", {"content": raw_content, "api_ids": ["c1", "c2"]})
    contents = backend.build_contents(history)
    assert [c.role for c in contents] == ["user", "model", "user"]
    assert contents[1].parts[0].thought_signature == b"sig"  # подпись размышлений сохранена
    responses = [p.function_response for p in contents[2].parts]
    assert [(r.name, r.id) for r in responses] == [("set_volume", "c1"), ("get_datetime", "c2")]
    assert responses[0].response == {"result": "Громкость установлена на 30%."}

    # история от другого бэкенда передаётся текстом (без подписей)
    foreign = backend.build_contents(history_with_parallel_calls())
    assert [c.role for c in foreign] == ["user", "model", "user"]
    assert "set_volume" in foreign[1].parts[0].text and foreign[2].parts[0].text.startswith("[Результат")

    cfg = backend.build_config("SYS", TOOL_SCHEMAS, allow_tools=True)
    assert cfg.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.AUTO
    assert cfg.automatic_function_calling.disable is True
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
    declaration = cfg.tools[0].function_declarations[0]
    assert declaration.name == "open_app" and declaration.parameters.required == ["name"]
    # Gemini отклоняет объект с пустыми properties — у функций без параметров они не пустые
    for item in cfg.tools[0].function_declarations:
        assert item.parameters.type == types.Type.OBJECT and item.parameters.properties, item.name
    volume = next(d for d in cfg.tools[0].function_declarations if d.name == "set_volume")
    assert volume.parameters.properties["level"].type == types.Type.INTEGER
    assert volume.parameters.properties["level"].maximum == 100
    assert backend.build_config("SYS", TOOL_SCHEMAS, allow_tools=False).tool_config.function_calling_config.mode \
        == types.FunctionCallingConfigMode.NONE
    config.set("gemini.model", "gemini-2.5-flash")
    assert backend.build_config("SYS", [], True).thinking_config.thinking_budget == 0

    def fake_stream(model, contents, config):
        yield types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(
            role="model", parts=[types.Part(text="Открываю")]))])
        yield types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name="open_app", args={"name": "блокнот"}, id="x1"),
                       thought_signature=b"s2")]))])

    backend.client.models.generate_content_stream = fake_stream
    pieces = []
    turn = backend.stream_chat("SYS", [{"role": "user", "content": "Открой блокнот"}], TOOL_SCHEMAS, True,
                               threading.Event(), pieces.append)
    assert pieces == ["Открываю"]
    assert turn.tool_calls[0].id == "x1" and turn.tool_calls[0].arguments == {"name": "блокнот"}
    assert turn.raw["data"]["content"].parts[1].thought_signature == b"s2"


def test_gemini_error_translation(config, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    from google.genai import errors

    backend = create_backend("gemini", config)
    region = errors.ClientError(400, {"error": {"code": 400, "message": "User location is not supported for the API use.",
                                                "status": "FAILED_PRECONDITION"}})
    assert "прокси" in backend._translate(region).hint
    quota = errors.ClientError(429, {"error": {"code": 429, "message": "Resource exhausted", "status": "RESOURCE_EXHAUSTED"}})
    assert "лимит" in backend._translate(quota).message
    bad_key = errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                                                 "status": "INVALID_ARGUMENT"}})
    assert "недействителен" in backend._translate(bad_key).message


_SELF_SIGNED_PEM = """-----BEGIN CERTIFICATE-----
MIIDEzCCAfugAwIBAgIUFJh2GwbqwwviX8hXV1FZi/ZqRkYwDQYJKoZIhvcNAQEL
BQAwGTEXMBUGA1UEAwwOSmFydmlzIFRlc3QgQ0EwHhcNMjYwOTI5MTEwNTQxWhcN
MzYwOTI2MTEwNTQxWjAZMRcwFQYDVQQDDA5KYXJ2aXMgVGVzdCBDQTCCASIwDQYJ
KoZIhvcNAQEBBQADggEPADCCAQoCggEBALJcKFrcmWHZgVB/FirIL7H7ohDD+CnQ
2B59kKSqOY6zZXnqKojn0szIHugalTkuxwlz2rbfNxtLRCQ3fA9GKcvdnvbrymM6
OI8MdMdp9elPEp4tyXsMxqxPkhNhvhwvM+sAbDNMvwgPxpbwZsrxp3tBr+/mMccJ
Lu0HQcGlBqOaSz+1KclKd2T6J9R50vJooj7h/txVsIK4Y73LBFslktOKHHNw8PhS
VDAaBqAGNsXm3m6gfGtXqiRcv/LRRBqSBM+7SvlokcWQfhCkIBvT8eCneoChy5N1
31sd/T2X80LvY9PYNppGp6j6PRsY4ih+yeSYBDiT51HdDU0QvrwbTjcCAwEAAaNT
MFEwHQYDVR0OBBYEFHLx+Fg/rt1gpK18BqwrW1joov0cMB8GA1UdIwQYMBaAFHLx
+Fg/rt1gpK18BqwrW1joov0cMA8GA1UdEwEB/wQFMAMBAf8wDQYJKoZIhvcNAQEL
BQADggEBAJkS2phgf2be3jVXQw6JRrMQHIHAkAiuOY1IS1U/WDOR2ASjDxh6dWtG
qbDSWR8YQdFdRUVnSCnlwtV85RKPW3fAPUC384MzS3+vksUfaeaaeeymlYwfgm3M
/01QMR4qisNHatrhdbzr7K6/YZruRu8Rd98QRuxBF97UBJlh5PkWw0f8SZhtnoXm
1fNoVAkAxdUkAX32hYY/jhdwA1Fsd7rK91ecc4an8O1mHrDeamPbaepWPlr4chnn
Le/v8w04lZixsAk0s8H/oHR9ncNfWRH12l2W3yiNoknUDbnS2jclExwaGOCWpOYi
hNRk7ieM067fZlh1q0h0IaW5H+I6hgY=
-----END CERTIFICATE-----
"""


# ─── Вызов инструмента, записанный моделью текстом ─────────────────────

@pytest.mark.parametrize("text, expected", [
    ('{"name": "get_datetime", "arguments": {}}', [("get_datetime", {})]),
    ('```json\n{"name": "open_app", "arguments": {"name": "блокнот"}}\n```', [("open_app", {"name": "блокнот"})]),
    ('<tool_call>\n{"name": "set_volume", "arguments": "{\\"level\\": 30}"}\n</tool_call>', [("set_volume", {"level": 30})]),
    ('<tool_call>{"name": "mute", "arguments": {"on": true}}</tool_call>\n<tool_call>{"name": "get_datetime", "arguments": {}}</tool_call>',
     [("mute", {"on": True}), ("get_datetime", {})]),
    ('[{"function": {"name": "note", "parameters": {"text": "молоко"}}}]', [("note", {"text": "молоко"})]),
    ('{"name": "format_disk", "arguments": {}}', []),  # неизвестный инструмент — не выполняем
    ('{"просто": "json"}', []),
    ("Обычный ответ", []),
])
def test_parse_text_tool_calls(text, expected):
    from jarvis.llm.ollama_backend import parse_text_tool_calls

    calls = parse_text_tool_calls(text, set(TOOLS))
    assert [(c.name, c.arguments) for c in calls] == expected


def test_ollama_rescues_textual_tool_call(config):
    import ollama

    backend = create_backend("ollama", config)

    def fake_chat(**kwargs):
        pieces = ['{"name": "get_', 'datetime", "argu', 'ments": {}}']
        return iter([ollama.ChatResponse(model="m", message=ollama.Message(role="assistant", content=p)) for p in pieces])

    backend.client.chat = fake_chat
    shown = []
    turn = backend.stream_chat("S", [{"role": "user", "content": "Который час?"}], TOOL_SCHEMAS, True,
                               threading.Event(), shown.append)
    assert shown == []  # JSON не показан и не озвучен
    assert [c.name for c in turn.tool_calls] == ["get_datetime"] and turn.text == ""

    def fake_text(**kwargs):
        return iter([ollama.ChatResponse(model="m", message=ollama.Message(role="assistant", content=p))
                     for p in ["\n", "{фигурные} ", "скобки в тексте"]])

    backend.client.chat = fake_text
    shown.clear()
    turn = backend.stream_chat("S", [{"role": "user", "content": "x"}], TOOL_SCHEMAS, True, threading.Event(), shown.append)
    assert "".join(shown).strip() == "{фигурные} скобки в тексте" and turn.tool_calls == []

    def fake_plain(**kwargs):
        return iter([ollama.ChatResponse(model="m", message=ollama.Message(role="assistant", content=p))
                     for p in ["Добрый ", "день, ", "сэр."]])

    backend.client.chat = fake_plain
    shown.clear()
    backend.stream_chat("S", [{"role": "user", "content": "x"}], TOOL_SCHEMAS, True, threading.Event(), shown.append)
    assert shown == ["Добрый ", "день, ", "сэр."]  # обычный текст идёт потоком без задержки
