"""Qwen через BazaarLink — OpenAI-совместимый шлюз к моделям Alibaba Qwen.

По умолчанию — бесплатная qwen/qwen3.7-flash:free (до 10 запросов в минуту и 50 в день).
Подойдёт и любой другой OpenAI-совместимый сервис (Alibaba Model Studio, OpenRouter и т. п.):
достаточно поменять qwen.base_url, qwen.model и ключ.
"""

from __future__ import annotations

import json
import logging
import os
import threading

import httpx

from .base import AssistantTurn, LLMBackend, LLMError, TextCallback, ToolCall
from .ollama_backend import ThinkFilter, looks_like_tool_json, parse_text_tool_calls

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.bazaarlink.ai/v1"
DEFAULT_MODEL = "qwen/qwen3.7-flash:free"
KEY_NAMES = ("BAZAARLINK_API_KEY", "QWEN_API_KEY")
KEY_HINT = (
    "Создайте ключ на https://bazaarlink.ai (раздел API Keys, ключ вида sk-bl-…) и вставьте его в "
    "«Настройки → Модели → Qwen» или впишите в файл .env строкой BAZAARLINK_API_KEY=ваш_ключ."
)
FREE_LIMIT_HINT = (
    "Подождите минуту (или до завтра — дневной лимит), либо выберите в «Настройки → Модели» платную "
    "qwen/qwen3.7-flash — она стоит доли копейки за запрос (нужен баланс на BazaarLink)."
)


def api_key() -> str:
    for name in KEY_NAMES:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def _complete_json(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except (TypeError, ValueError):
        return False


def parse_arguments(text: str) -> dict:
    """Аргументы вызова из JSON-строки; лишний «хвост» после объекта отбрасывается."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        try:
            value, _end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            log.warning("Не удалось разобрать аргументы вызова: %s", text[:200])
            return {}
    return value if isinstance(value, dict) else {}


class ToolCallCollector:
    """Собирает вызовы инструментов из потоковых фрагментов delta.tool_calls."""

    def __init__(self) -> None:
        self._slots: dict[int, dict] = {}

    def feed(self, fragments: list) -> None:
        for fragment in fragments or []:
            if not isinstance(fragment, dict):
                continue
            index = fragment.get("index")
            call_id = fragment.get("id") or ""
            if index is None:  # некоторые шлюзы не присылают index
                index = next((i for i, s in self._slots.items() if call_id and s["id"] == call_id),
                             len(self._slots) if call_id or not self._slots else max(self._slots))
            slot = self._slots.setdefault(int(index), {"id": "", "name": "", "arguments": ""})
            if call_id:
                slot["id"] = call_id
            function = fragment.get("function") or {}
            name = function.get("name")
            if name:
                slot["name"] = name if not slot["name"] or slot["name"] == name else slot["name"] + name
            arguments = function.get("arguments")
            if isinstance(arguments, dict):  # объект вместо строки
                slot["arguments"] = json.dumps(arguments, ensure_ascii=False)
            elif arguments:
                if _complete_json(slot["arguments"]) and _complete_json(arguments):
                    slot["arguments"] = arguments  # прислали аргументы целиком ещё раз
                else:
                    slot["arguments"] += arguments

    def calls(self) -> list[ToolCall]:
        result = []
        for _index, slot in sorted(self._slots.items()):
            if not slot["name"]:
                continue
            call = ToolCall(name=slot["name"], arguments=parse_arguments(slot["arguments"]))
            if slot["id"]:
                call.id = slot["id"]
            result.append(call)
        return result


class QwenBackend(LLMBackend):
    name = "qwen"
    title = "Qwen"

    def __init__(self, config):
        super().__init__(config)
        key = api_key()
        if not key:
            raise LLMError("Не задан ключ API для Qwen (BAZAARLINK_API_KEY).", KEY_HINT)
        self.base_url = str(config.get("qwen.base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        headers = {"Authorization": f"Bearer {key}", "X-Title": "Jarvis"}
        if not config.get("qwen.free_fallback", False):
            # Кончился бесплатный лимит — пусть будет ошибка 429, а не тихие платные запросы.
            headers["X-Free-Fallback"] = "false"
        self.proxy = str(config.get("qwen.proxy", "") or "").strip()
        timeout = float(config.get("llm.timeout", 300))
        try:
            self.client = httpx.Client(base_url=self.base_url, headers=headers, proxy=self.proxy or None,
                                       timeout=httpx.Timeout(timeout, connect=15.0))
        except ImportError as exc:  # socks5-прокси требует пакет socksio
            raise LLMError(f"Не удалось настроить прокси {self.proxy}: {exc}",
                           'Выполните: pip install "httpx[socks]"') from None
        self._thinking_param = True

    @property
    def model(self) -> str:
        return str(self.config.get("qwen.model") or DEFAULT_MODEL).strip()

    # ─── запрос ───
    @staticmethod
    def build_messages(system: str, messages: list[dict]) -> list[dict]:
        result: list[dict] = [{"role": "system", "content": system}]
        call_ids: set[str] = set()
        for message in messages:
            role = message["role"]
            if role == "user":
                result.append({"role": "user", "content": message["content"]})
            elif role == "assistant":
                item: dict = {"role": "assistant", "content": message.get("content") or ""}
                calls = message.get("tool_calls") or []
                if calls:
                    item["tool_calls"] = [
                        {"id": c.id, "type": "function",
                         "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                        for c in calls
                    ]
                    call_ids.update(c.id for c in calls)
                if item["content"] or calls:
                    result.append(item)
            elif role == "tool":
                if message.get("tool_call_id") in call_ids:
                    result.append({"role": "tool", "tool_call_id": message["tool_call_id"],
                                   "content": message["content"]})
                else:  # вызов не попал в окно истории — передаём результат текстом
                    result.append({"role": "user", "content": f"[Результат {message.get('name')}]: {message['content']}"})
        return result

    @staticmethod
    def build_tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": t} for t in tools]

    def build_body(self, system: str, messages: list[dict], tools: list[dict], allow_tools: bool) -> dict:
        body: dict = {"model": self.model, "messages": self.build_messages(system, messages), "stream": True}
        temperature = self.config.get("qwen.temperature")
        if temperature is not None:
            body["temperature"] = float(temperature)
        if tools:
            body["tools"] = self.build_tools(tools)
            body["tool_choice"] = "auto" if allow_tools else "none"
        if self._thinking_param and "qwen" in self.model.lower():
            # «Размышления» Qwen3 сильно замедляют голосовой ответ — по умолчанию выключены.
            body["enable_thinking"] = bool(self.config.get("qwen.thinking", False))
        return body

    # ─── ошибки ───
    def _status_error(self, status: int, response: httpx.Response) -> LLMError:
        message, code = "", ""
        try:
            payload = response.json()
            error = payload.get("error", payload) if isinstance(payload, dict) else {}
            if isinstance(error, dict):
                message = str(error.get("message") or "")
                code = str(error.get("code") or "")
            else:
                message = str(error)
        except ValueError:
            message = response.text[:300]
        return self._error(status, message, code)

    def _error(self, status: int, message: str, code: str = "") -> LLMError:
        error = self._describe(status, message, code)
        error.status = status
        error.detail = message
        return error

    def _describe(self, status: int, message: str, code: str) -> LLMError:
        low = f"{message} {code}".lower()
        if not status and "rate_limit" in low:
            status = 429
        if status == 401:
            return LLMError("Ключ API для Qwen недействителен или отключён (401).", KEY_HINT)
        if status == 402 or "insufficient" in low:
            return LLMError("На счёте BazaarLink не хватает средств для этой модели (402).",
                            "Пополните баланс на https://bazaarlink.ai или выберите бесплатную модель "
                            f"{DEFAULT_MODEL} в «Настройки → Модели».")
        if status == 404 or "model_not_found" in low or "unknown_model" in low:
            return LLMError(f"Модель «{self.model}» не найдена в BazaarLink.",
                            f"Выберите модель в «Настройки → Модели → Qwen», например {DEFAULT_MODEL} "
                            "или qwen/qwen3.7-flash.")
        if status == 429:
            if self.model.endswith(":free"):
                return LLMError("Исчерпан бесплатный лимит Qwen: 10 запросов в минуту и 50 в день.", FREE_LIMIT_HINT)
            return LLMError("Слишком много запросов к Qwen (429).", "Подождите немного и повторите.")
        if status == 403:
            return LLMError(f"Доступ к Qwen запрещён (403): {message[:200]}",
                            "Проверьте ключ и ограничения в личном кабинете BazaarLink.")
        if status >= 500:
            return LLMError(f"Сервер BazaarLink временно недоступен ({status}).", "Повторите запрос через минуту.")
        detail = f": {message[:300]}" if message else ""
        if not status:
            return LLMError(f"Qwen прервал ответ{detail}", "Повторите запрос.")
        return LLMError(f"Qwen отклонил запрос ({status}){detail}")

    def _transport_error(self, exc: Exception) -> LLMError:
        if isinstance(exc, httpx.ProxyError) or (self.proxy and isinstance(exc, httpx.ConnectError)):
            return LLMError(f"Не удалось подключиться к Qwen через прокси {self.proxy}: {exc}",
                            "Проверьте, что прокси/VPN запущен и адрес в config.yaml → qwen.proxy верный.")
        if isinstance(exc, httpx.TimeoutException):
            return LLMError(f"Qwen не ответил за {self.config.get('llm.timeout', 300)} с.", "Повторите запрос позже.")
        if isinstance(exc, httpx.ConnectError):
            host = httpx.URL(self.base_url).host
            return LLMError(f"Нет соединения с {host}.",
                            "Проверьте интернет. Если сайт недоступен из вашей сети — укажите прокси в "
                            "config.yaml → qwen.proxy.")
        return LLMError(f"Ошибка соединения с Qwen: {exc}")

    # ─── запросы ───
    def check(self) -> str | None:
        """Проверка ключа и модели по списку моделей — бесплатно, лимит запросов не тратится."""
        try:
            response = self.client.get("/models")
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from None
        if response.status_code == 404:
            return None  # сервис без списка моделей — проверим первым запросом
        if response.status_code >= 400:
            raise self._status_error(response.status_code, response)
        try:
            items = response.json().get("data") or []
        except (ValueError, AttributeError):
            return None
        ids: set[str] = set()
        for item in items:
            if isinstance(item, dict):
                ids.add(str(item.get("id") or ""))
                ids.update(str(alias) for alias in item.get("aliases") or [])
        ids.discard("")
        if ids and self.model not in ids:
            qwen = sorted(i for i in ids if "qwen" in i.lower() and "/" in i)[:8]
            raise LLMError(f"Модели «{self.model}» нет в списке BazaarLink.",
                           "Выберите модель в «Настройки → Модели → Qwen»"
                           + (f". Доступны, например: {', '.join(qwen)}." if qwen else "."))
        return None

    def stream_chat(self, system: str, messages: list[dict], tools: list[dict], allow_tools: bool,
                    cancel: threading.Event, on_text: TextCallback) -> AssistantTurn:
        try:
            return self._stream(system, messages, tools, allow_tools, cancel, on_text)
        except LLMError as exc:
            if self._thinking_param and getattr(exc, "status", 0) == 400 and \
                    "thinking" in str(getattr(exc, "detail", "")).lower():
                log.info("Сервис не принимает enable_thinking — повторяю без него")
                self._thinking_param = False
                return self.stream_chat(system, messages, tools, allow_tools, cancel, on_text)
            raise
        except httpx.HTTPError as exc:
            raise self._transport_error(exc) from None

    def _stream(self, system, messages, tools, allow_tools, cancel, on_text) -> AssistantTurn:
        body = self.build_body(system, messages, tools, allow_tools)
        think_filter = ThinkFilter()
        collector = ToolCallCollector()
        text_parts: list[str] = []
        # Как и для Ollama: начало ответа, похожее на JSON, придерживаем — это может быть
        # вызов инструмента, записанный текстом, его нельзя показывать и озвучивать.
        held: list[str] = []
        holding = allow_tools and bool(tools)

        def emit(text: str) -> None:
            nonlocal holding
            if holding:
                held.append(text)
                joined = "".join(held)
                if not joined.strip() or looks_like_tool_json(joined):
                    return
                holding = False
                text = joined
                held.clear()
            text_parts.append(text)
            on_text(text)

        with self.client.stream("POST", "/chat/completions", json=body,
                                headers={"Accept": "text/event-stream"}) as response:
            if response.status_code >= 400:
                response.read()
                raise self._status_error(response.status_code, response)
            for line in response.iter_lines():
                if cancel.is_set():
                    break
                if not line.startswith("data:"):
                    continue  # пустые строки и комментарии-«пинги» SSE
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error"):  # сбой уже после начала ответа
                    error = chunk["error"] if isinstance(chunk["error"], dict) else {"message": str(chunk["error"])}
                    status = error.get("code") if isinstance(error.get("code"), int) else 0
                    raise self._error(status, str(error.get("message") or ""), str(error.get("code") or ""))
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or choice.get("message") or {}
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        visible = think_filter.feed(content)
                        if visible:
                            emit(visible)
                    collector.feed(delta.get("tool_calls"))
        calls = collector.calls()
        rest = think_filter.flush()
        if rest and not cancel.is_set():
            emit(rest)
        if held and not cancel.is_set():
            pending = "".join(held)
            rescued = [] if calls else parse_text_tool_calls(pending, {t["name"] for t in tools})
            if rescued:
                log.info("Модель записала вызов инструмента текстом — выполняю его: %s", pending[:200])
                calls = rescued
            elif pending.strip():
                text_parts.append(pending)
                on_text(pending)
        return AssistantTurn(text="".join(text_parts).strip(), tool_calls=calls if allow_tools else [])

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
