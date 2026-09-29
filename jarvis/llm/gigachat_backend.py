"""GigaChat API (Сбер) — бесплатный режим Freemium для физлиц."""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading

import httpx

from .base import AssistantTurn, LLMBackend, LLMError, TextCallback, ToolCall, pair_tool_results

log = logging.getLogger(__name__)

KEY_HINT = (
    "Получите ключ авторизации: https://developers.sber.ru/studio → проект GigaChat API → "
    "«Настройки API» → «Получить ключ». Затем впишите его в файл .env строкой "
    "GIGACHAT_CREDENTIALS=ваш_ключ и перезапустите Джарвиса."
)
CERT_HINT = (
    "Нужен корневой сертификат НУЦ Минцифры. Проще всего: запустите «python download_models.py --cert» — "
    "он скачает сертификат в certs\\russian_trusted_root_ca.crt (путь уже указан в config.yaml → "
    "gigachat.ca_bundle_file). Либо установите сертификат в Windows с https://www.gosuslugi.ru/crt. "
    "Крайний вариант (небезопасно): gigachat.verify_ssl_certs: false."
)


def _is_cert_error(exc: BaseException) -> bool:
    current: BaseException | None = exc
    while current is not None:
        if isinstance(current, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(current):
            return True
        current = current.__cause__ or current.__context__
    return False


def build_ssl_context(ca_file) -> ssl.SSLContext:
    """Системное хранилище (в Windows — сертификаты Windows) + certifi + сертификат Минцифры."""
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    if ca_file and os.path.isfile(ca_file):
        data = open(ca_file, "rb").read()
        if b"-----BEGIN CERTIFICATE-----" in data:
            context.load_verify_locations(cadata=data.decode("ascii", errors="ignore"))
        else:  # .cer в двоичном формате DER
            context.load_verify_locations(cadata=data)
    return context


class GigaChatBackend(LLMBackend):
    name = "gigachat"
    title = "GigaChat"

    def __init__(self, config):
        super().__init__(config)
        credentials = (os.environ.get("GIGACHAT_CREDENTIALS") or "").strip()
        if not credentials:
            raise LLMError("Не задан ключ GigaChat (GIGACHAT_CREDENTIALS в файле .env).", KEY_HINT)
        try:
            from gigachat import GigaChat
            from gigachat import exceptions as giga_exceptions
            from gigachat import models as giga_models
        except ImportError as exc:
            raise LLMError("Библиотека gigachat не установлена.", "Выполните: pip install -r requirements.txt") from exc
        self._models = giga_models
        self._exceptions = giga_exceptions
        kwargs = {
            "credentials": credentials,
            "scope": config.get("gigachat.scope", "GIGACHAT_API_PERS"),
            "model": self.model,
            "timeout": float(config.get("llm.timeout", 300)),
        }
        if config.get("gigachat.verify_ssl_certs", True) is False:
            kwargs["verify_ssl_certs"] = False
        else:
            ca_file = config.resolve_path(config.get("gigachat.ca_bundle_file"))
            try:
                kwargs["ssl_context"] = build_ssl_context(ca_file)
            except (OSError, ssl.SSLError) as exc:
                raise LLMError(f"Не удалось прочитать сертификат {ca_file}: {exc}", CERT_HINT) from None
        self.client = GigaChat(**kwargs)

    @property
    def model(self) -> str:
        return str(self.config.get("gigachat.model", "GigaChat-2"))

    # ─── сообщения ───
    def build_messages(self, system: str, messages: list[dict]) -> list:
        M = self._models.Messages
        results = pair_tool_results(messages)
        used: set[str] = set()
        out = [M(role="system", content=system)]
        for message in messages:
            role = message["role"]
            if role == "user":
                out.append(M(role="user", content=message["content"]))
            elif role == "assistant":
                calls = message.get("tool_calls") or []
                raw = self.raw_for(message) or {}
                if not calls:
                    out.append(M(role="assistant", content=message.get("content") or ""))
                    continue
                # GigaChat понимает один вызов функции на сообщение — раскладываем по парам.
                for index, call in enumerate(calls):
                    out.append(M(
                        role="assistant",
                        content=(message.get("content") or "") if index == 0 else "",
                        function_call=self._models.FunctionCall(name=call.name, arguments=call.arguments),
                        functions_state_id=raw.get("functions_state_id") if index == 0 else None,
                    ))
                    result = results.get(call.id)
                    content = result["content"] if result else "Результат недоступен."
                    out.append(M(role="function", name=call.name,
                                 content=json.dumps({"result": content}, ensure_ascii=False)))
                    used.add(call.id)
            elif role == "tool" and message.get("tool_call_id") not in used:
                # Результат без своего вызова (обрезанная история) — передаём текстом.
                out.append(M(role="user", content=f"[Результат {message.get('name')}]: {message['content']}"))
        return out

    def build_functions(self, tools: list[dict]) -> list:
        functions = []
        for item in tools:
            parameters = item["parameters"]
            properties = {}
            for key, spec in parameters.get("properties", {}).items():
                prop = {"type": spec.get("type", "string"), "description": spec.get("description", "")}
                if "enum" in spec:
                    prop["enum"] = [str(v) for v in spec["enum"]]
                properties[key] = prop
            functions.append(self._models.Function(
                name=item["name"],
                description=item["description"],
                parameters=self._models.FunctionParameters(
                    type="object", properties=properties, required=list(parameters.get("required", [])),
                ),
            ))
        return functions

    # ─── ошибки ───
    def _translate(self, exc: Exception) -> LLMError:
        errors = self._exceptions
        if _is_cert_error(exc):
            return LLMError("Ошибка SSL-сертификата при подключении к GigaChat.", CERT_HINT)
        if isinstance(exc, errors.AuthenticationError):
            return LLMError("GigaChat отклонил ключ авторизации (401).",
                            "Проверьте GIGACHAT_CREDENTIALS в .env (ключ целиком, без кавычек и пробелов) "
                            "и gigachat.scope в config.yaml (для физлиц — GIGACHAT_API_PERS). " + KEY_HINT)
        status = getattr(exc, "status_code", None)
        if status == 402:
            return LLMError("Закончились бесплатные токены GigaChat для этой модели.",
                            "Выберите другую модель в config.yaml → gigachat.model (например GigaChat-2) "
                            "или переключитесь на Ollama/Gemini.")
        if isinstance(exc, errors.RateLimitError):
            return LLMError("Слишком много запросов к GigaChat (429).",
                            "В режиме Freemium запросы идут в один поток — подождите несколько секунд и повторите.")
        if isinstance(exc, errors.NotFoundError):
            return LLMError(f"Модель «{self.model}» недоступна в GigaChat (404).",
                            "Укажите в config.yaml → gigachat.model одну из: GigaChat-2, GigaChat-2-Pro, "
                            "GigaChat-2-Max, GigaChat-3-Ultra.")
        if isinstance(exc, errors.ResponseError):
            content = getattr(exc, "content", b"") or b""
            detail = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
            return LLMError(f"GigaChat вернул ошибку {status}: {detail[:300]}")
        if isinstance(exc, httpx.TimeoutException):
            return LLMError(f"GigaChat не ответил за {self.config.get('llm.timeout', 300)} с.", "Повторите запрос позже.")
        if isinstance(exc, httpx.HTTPError):
            return LLMError(f"Нет соединения с GigaChat: {exc}", "Проверьте подключение к интернету.")
        return LLMError(f"Ошибка GigaChat: {type(exc).__name__}: {exc}")

    # ─── запросы ───
    def check(self) -> str | None:
        try:
            available = [m.id_ for m in self.client.get_models().data]
        except Exception as exc:
            raise self._translate(exc) from None
        if available and self.model not in available:
            log.info("Модели GigaChat в аккаунте: %s", available)
        return None

    def stream_chat(self, system: str, messages: list[dict], tools: list[dict], allow_tools: bool,
                    cancel: threading.Event, on_text: TextCallback) -> AssistantTurn:
        payload = {
            "model": self.model,
            "messages": self.build_messages(system, messages),
            "temperature": max(0.01, float(self.config.get("gigachat.temperature", 0.4))),
        }
        if tools:
            payload["functions"] = self.build_functions(tools)
            payload["function_call"] = "auto" if allow_tools else "none"
        chat = self._models.Chat(**payload)

        text_parts: list[str] = []
        call_name, call_args, state_id = None, {}, None
        try:
            stream = self.client.stream(chat)
            try:
                for chunk in stream:
                    if cancel.is_set():
                        break
                    for choice in chunk.choices:
                        delta = choice.delta
                        if delta.content:
                            text_parts.append(delta.content)
                            on_text(delta.content)
                        if delta.function_call is not None:
                            call_name = delta.function_call.name or call_name
                            if isinstance(delta.function_call.arguments, dict):
                                call_args.update(delta.function_call.arguments)
                        if getattr(delta, "functions_state_id", None):
                            state_id = delta.functions_state_id
            finally:
                close = getattr(stream, "close", None)
                if close:
                    close()
        except LLMError:
            raise
        except Exception as exc:
            raise self._translate(exc) from None

        calls = [ToolCall(name=call_name, arguments=call_args)] if (call_name and allow_tools) else []
        return AssistantTurn(
            text="".join(text_parts).strip(),
            tool_calls=calls,
            raw={"backend": self.name, "data": {"functions_state_id": state_id}},
        )

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

