"""Google Gemini API (бесплатный ключ из Google AI Studio)."""

from __future__ import annotations

import logging
import os
import threading

import httpx

from .base import AssistantTurn, LLMBackend, LLMError, TextCallback, ToolCall, tool_calls_as_text

log = logging.getLogger(__name__)

KEY_HINT = (
    "Получите бесплатный ключ на https://aistudio.google.com/apikey и впишите его в файл .env строкой "
    "GEMINI_API_KEY=ваш_ключ, затем перезапустите Джарвиса."
)
REGION_HINT = (
    "Gemini API недоступен из вашего региона. Укажите прокси в config.yaml → gemini.proxy "
    "(например http://127.0.0.1:10809 или socks5://127.0.0.1:10808) или включите VPN."
)


class GeminiBackend(LLMBackend):
    name = "gemini"
    title = "Gemini"

    def __init__(self, config):
        super().__init__(config)
        api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
        if not api_key:
            raise LLMError("Не задан ключ Gemini (GEMINI_API_KEY в файле .env).", KEY_HINT)
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError as exc:
            raise LLMError("Библиотека google-genai не установлена.", "Выполните: pip install -r requirements.txt") from exc
        self.types = types
        self.errors = errors
        http_options = {"timeout": int(float(config.get("llm.timeout", 300)) * 1000)}
        proxy = str(config.get("gemini.proxy", "") or "").strip()
        if proxy:
            http_options["client_args"] = {"proxy": proxy}
        try:
            self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(**http_options))
        except ImportError as exc:  # socks5-прокси требует пакет socksio
            raise LLMError(f"Не удалось настроить прокси {proxy}: {exc}", "Выполните: pip install \"httpx[socks]\"") from None
        self._thinking_enabled = True

    @property
    def model(self) -> str:
        return str(self.config.get("gemini.model", "gemini-flash-latest"))

    # ─── сообщения ───
    def build_contents(self, messages: list[dict]) -> list:
        types = self.types
        contents: list = []
        native_ids: set[str] = set()  # вызовы, сделанные самим Gemini
        api_ids: set[str] = set()     # из них — с id от API

        def add(role: str, parts: list) -> None:
            if not parts:
                return
            if contents and contents[-1].role == role:
                contents[-1] = types.Content(role=role, parts=list(contents[-1].parts or []) + parts)
            else:
                contents.append(types.Content(role=role, parts=parts))

        for message in messages:
            role = message["role"]
            if role == "user":
                add("user", [types.Part(text=message["content"])])
            elif role == "assistant":
                raw = self.raw_for(message)
                if raw is not None and raw.get("content") is not None:
                    api_ids.update(raw.get("api_ids", []))
                    native_ids.update(c.id for c in message.get("tool_calls") or [])
                    add("model", list(raw["content"].parts or []))
                else:
                    # Ответ другого бэкенда: вызовы инструментов передаём текстом (без подписей Gemini).
                    text = tool_calls_as_text(message)
                    if text:
                        add("model", [types.Part(text=text)])
            elif role == "tool":
                call_id = message.get("tool_call_id")
                if call_id in native_ids:
                    part = types.Part.from_function_response(name=message.get("name", ""),
                                                             response={"result": message["content"]})
                    if call_id in api_ids:
                        part.function_response.id = call_id
                    add("user", [part])
                else:
                    add("user", [types.Part(text=f"[Результат {message.get('name')}]: {message['content']}")])
        return contents

    @staticmethod
    def gemini_parameters(parameters: dict) -> dict:
        """Gemini отклоняет объект с пустыми properties («should be non-empty for OBJECT type»),
        поэтому функциям без параметров даём один необязательный параметр. Реестр
        инструментов лишние аргументы игнорирует."""
        if parameters.get("properties"):
            return parameters
        return {
            "type": "object",
            "properties": {"note": {"type": "string", "description": "Не требуется, можно не передавать."}},
            "required": [],
        }

    def build_tools(self, tools: list[dict]) -> list:
        types = self.types
        declarations = [
            types.FunctionDeclaration(name=t["name"], description=t["description"],
                                      parameters=self.gemini_parameters(t["parameters"]))
            for t in tools
        ]
        return [types.Tool(function_declarations=declarations)]

    def _thinking_config(self):
        level = str(self.config.get("gemini.thinking_level", "") or "").strip().lower()
        if not level or not self._thinking_enabled:
            return None
        model = self.model.lower()
        if "2.5" in model or "2.0" in model:
            if "pro" in model:
                return None
            budget = {"minimal": 0, "low": 0, "medium": 4096, "high": -1}.get(level, 0)
            return self.types.ThinkingConfig(thinking_budget=budget)
        return self.types.ThinkingConfig(thinking_level=level)

    def build_config(self, system: str, tools: list[dict], allow_tools: bool):
        types = self.types
        kwargs = {
            "system_instruction": system,
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if tools:
            kwargs["tools"] = self.build_tools(tools)
            kwargs["tool_config"] = types.ToolConfig(
                function_calling_config=types.FunctionCallingConfig(mode="AUTO" if allow_tools else "NONE")
            )
        temperature = self.config.get("gemini.temperature")
        if temperature is not None:
            kwargs["temperature"] = float(temperature)
        thinking = self._thinking_config()
        if thinking is not None:
            kwargs["thinking_config"] = thinking
        return types.GenerateContentConfig(**kwargs)

    # ─── ошибки ───
    def _translate(self, exc: Exception) -> LLMError:
        if isinstance(exc, self.errors.APIError):
            code = getattr(exc, "code", None)
            message = str(getattr(exc, "message", "") or exc)
            low = message.lower()
            if "location is not supported" in low or "user location" in low:
                return LLMError("Gemini API недоступен в вашем регионе.", REGION_HINT)
            if "api key" in low or "api_key" in low:
                return LLMError("Ключ Gemini недействителен.", KEY_HINT)
            if code == 403:
                return LLMError(f"Доступ к Gemini запрещён (403): {message[:200]}",
                                "Проверьте ключ и ограничения проекта в Google AI Studio. " + REGION_HINT)
            if code == 404:
                return LLMError(f"Модель «{self.model}» не найдена в Gemini API.",
                                "Укажите в config.yaml → gemini.model доступную модель, например "
                                "gemini-flash-latest или gemini-flash-lite-latest.")
            if code == 429:
                return LLMError("Исчерпан лимит бесплатного тарифа Gemini (429).",
                                "Подождите минуту (или до завтра — дневной лимит), либо выберите "
                                "gemini-flash-lite-latest, у неё лимиты больше.")
            if code and code >= 500:
                return LLMError(f"Сервер Gemini временно недоступен ({code}).", "Повторите запрос через минуту.")
            return LLMError(f"Ошибка Gemini ({code}): {message[:300]}")
        proxy = str(self.config.get("gemini.proxy", "") or "").strip()
        if isinstance(exc, httpx.ProxyError) or (proxy and isinstance(exc, httpx.ConnectError)):
            return LLMError(f"Не удалось подключиться к Gemini через прокси {proxy}: {exc}",
                            "Проверьте, что прокси/VPN запущен и адрес в config.yaml → gemini.proxy верный.")
        if isinstance(exc, httpx.TimeoutException):
            return LLMError(f"Gemini не ответил за {self.config.get('llm.timeout', 300)} с.", "Повторите запрос позже.")
        if isinstance(exc, httpx.HTTPError):
            return LLMError(f"Нет соединения с Gemini: {exc}", "Проверьте интернет. " + REGION_HINT)
        return LLMError(f"Ошибка Gemini: {type(exc).__name__}: {exc}")

    def _is_thinking_error(self, exc: Exception) -> bool:
        return isinstance(exc, self.errors.ClientError) and "think" in str(exc).lower()

    # ─── запросы ───
    def check(self) -> str | None:
        try:
            self.client.models.get(model=self.model)
        except Exception as exc:
            raise self._translate(exc) from None
        return None

    def stream_chat(self, system: str, messages: list[dict], tools: list[dict], allow_tools: bool,
                    cancel: threading.Event, on_text: TextCallback) -> AssistantTurn:
        try:
            return self._stream(system, messages, tools, allow_tools, cancel, on_text)
        except Exception as exc:
            if self._is_thinking_error(exc) and self._thinking_enabled:
                log.info("Модель %s не принимает thinking_level — отключаю настройку: %s", self.model, exc)
                self._thinking_enabled = False
                try:
                    return self._stream(system, messages, tools, allow_tools, cancel, on_text)
                except Exception as retry_exc:
                    raise self._translate(retry_exc) from None
            if isinstance(exc, LLMError):
                raise
            raise self._translate(exc) from None

    def _stream(self, system, messages, tools, allow_tools, cancel, on_text) -> AssistantTurn:
        types = self.types
        stream = self.client.models.generate_content_stream(
            model=self.model,
            contents=self.build_contents(messages),
            config=self.build_config(system, tools, allow_tools),
        )
        parts: list = []
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        api_ids: list[str] = []
        blocked = ""
        try:
            for chunk in stream:
                if cancel.is_set():
                    break
                candidates = chunk.candidates or []
                if not candidates:
                    feedback = getattr(chunk, "prompt_feedback", None)
                    if feedback is not None and getattr(feedback, "block_reason", None):
                        blocked = str(feedback.block_reason)
                    continue
                content = candidates[0].content
                for part in (content.parts if content and content.parts else []):
                    parts.append(part)
                    if part.function_call is not None:
                        call = part.function_call
                        call_id = call.id or None
                        tool_call = ToolCall(name=call.name or "", arguments=dict(call.args or {}))
                        if call_id:
                            tool_call.id = call_id
                            api_ids.append(call_id)
                        calls.append(tool_call)
                    elif part.text and not part.thought:
                        text_parts.append(part.text)
                        on_text(part.text)
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        if blocked and not parts:
            raise LLMError(f"Gemini отказался отвечать на запрос (фильтр безопасности: {blocked}).")
        if not allow_tools:
            # Вызовы без ответов сломали бы историю — оставляем только текст.
            parts = [p for p in parts if p.function_call is None]
            calls, api_ids = [], []
        raw = None
        if parts and not cancel.is_set():
            raw = {"backend": self.name, "data": {"content": types.Content(role="model", parts=parts), "api_ids": api_ids}}
        return AssistantTurn(text="".join(text_parts).strip(), tool_calls=calls, raw=raw)
