"""Ollama — локальная модель (по умолчанию qwen3:8b)."""

from __future__ import annotations

import json
import logging
import re
import threading
from urllib.parse import urlparse

import httpx

from .base import AssistantTurn, LLMBackend, LLMError, TextCallback, ToolCall

log = logging.getLogger(__name__)

_DOWNLOAD_HINT = "Если Ollama не установлена — скачайте её с https://ollama.com/download"
FAST_MODELS_HINT = "qwen3:4b-instruct или qwen3:8b"


def _partial_suffix(text: str, tag: str) -> int:
    """Длина конца text, который может быть началом тега tag."""
    for size in range(min(len(text), len(tag) - 1), 0, -1):
        if tag.startswith(text[-size:]):
            return size
    return 0


class ThinkFilter:
    """Вырезает <think>…</think> из потока: старые версии Ollama игнорируют think=False.
    inside=True — размышления уже начаты шаблоном модели (открывающего тега в ответе не будет)."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self, inside: bool = False) -> None:
        self._buf = ""
        self._inside = inside
        self._started = False
        self.stray_close = False  # встретился «</think>» без открывающего — модель всё-таки размышляла

    def _start(self, text: str) -> str:
        if not self._started:
            text = text.lstrip()
            self._started = bool(text)
        return text

    def feed(self, text: str) -> str:
        self._buf += text
        out: list[str] = []
        while self._buf:
            if self._inside:
                index = self._buf.find(self.CLOSE)
                if index < 0:
                    keep = _partial_suffix(self._buf, self.CLOSE)
                    self._buf = self._buf[len(self._buf) - keep:] if keep else ""
                    break
                self._buf = self._buf[index + len(self.CLOSE):]
                self._inside = False
            else:
                index = self._buf.find(self.OPEN)
                close = self._buf.find(self.CLOSE)
                if close >= 0 and (index < 0 or close < index):
                    self.stray_close = True  # рассуждения без <think>: всё до «</think>» — не ответ
                    out.clear()
                    self._buf = self._buf[close + len(self.CLOSE):]
                    continue
                if index < 0:
                    keep = max(_partial_suffix(self._buf, self.OPEN), _partial_suffix(self._buf, self.CLOSE))
                    out.append(self._buf[:len(self._buf) - keep])
                    self._buf = self._buf[len(self._buf) - keep:]
                    break
                out.append(self._buf[:index])
                self._buf = self._buf[index + len(self.OPEN):]
                self._inside = True
        return self._start("".join(out))

    def flush(self) -> str:
        rest = "" if self._inside else self._buf
        self._buf = ""
        return self._start(rest)


_GENERATION_THINK_RE = re.compile(r"assistant(?:\|>|｜>)?(?:\n|\\n)<think>")


def template_always_thinks(template: str) -> bool:
    """Шаблон сам открывает <think> в начале каждого ответа (сразу после «assistant») и не умеет
    выключать размышления — так устроены «думающие» модели, например qwen3:4b = qwen3:4b-thinking.
    Упоминание <think> в разборе прошлых ответов (как у qwen3:4b-instruct) не в счёт."""
    return bool(_GENERATION_THINK_RE.search(template)) and not re.search(r"\.Think\b", template)


def strip_think(text: str) -> str:
    """Текст без блоков <think>…</think>."""
    think_filter = ThinkFilter()
    return (think_filter.feed(text) + think_filter.flush()).strip()


_JSON_START_RE = re.compile(r"^\s*(?:```(?:json)?\s*)?(?:<tool_call>\s*)?[\[{]")


def looks_like_tool_json(text: str) -> bool:
    """Начало ответа похоже (или ещё может оказаться) вызовом инструмента, записанным текстом."""
    stripped = text.lstrip()
    return bool(_JSON_START_RE.match(text)) or any(p.startswith(stripped) for p in ("```json", "<tool_call>"))


def parse_text_tool_calls(text: str, known: set[str]) -> list[ToolCall]:
    """Небольшие локальные модели иногда пишут вызов инструмента текстом:
    {"name": "open_app", "arguments": {...}} (иногда в ```json``` или <tool_call>).
    Если это вызов известного инструмента — превращаем его в настоящий вызов."""
    body = re.sub(r"```(?:json)?|</?tool_call>", "\n", text).strip()
    decoder = json.JSONDecoder()
    items: list = []
    position = 0
    while position < len(body):
        while position < len(body) and body[position] in " \t\r\n,":
            position += 1
        if position >= len(body):
            break
        try:
            value, position = decoder.raw_decode(body, position)
        except json.JSONDecodeError:
            return []
        items.extend(value if isinstance(value, list) else [value])
    calls: list[ToolCall] = []
    for item in items:
        if not isinstance(item, dict):
            return []
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name")
        arguments = function.get("arguments", function.get("parameters", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                return []
        if name not in known or not isinstance(arguments, dict):
            return []
        calls.append(ToolCall(name=name, arguments=arguments))
    return calls


class OllamaBackend(LLMBackend):
    name = "ollama"
    title = "Ollama"

    def __init__(self, config):
        super().__init__(config)
        try:
            import ollama
        except ImportError as exc:
            raise LLMError("Библиотека ollama не установлена.", "Выполните: pip install -r requirements.txt") from exc
        self._ollama = ollama
        self.host = str(config.get("ollama.host", "http://localhost:11434")).rstrip("/")
        timeout = float(config.get("llm.timeout", 300))
        kwargs = {"timeout": httpx.Timeout(timeout, connect=5.0)}
        if (urlparse(self.host).hostname or "localhost") in ("localhost", "127.0.0.1", "::1"):
            kwargs["trust_env"] = False  # системный прокси не должен перехватывать localhost
        self.client = ollama.Client(host=self.host, **kwargs)
        self._think_supported = True
        self._main_has_vision: bool | None = None
        self._thinking_checked_for = ""
        self._always_thinks = False

    @property
    def model(self) -> str:
        return str(self.config.get("ollama.model", "qwen3:8b"))

    # ─── сообщения ───
    def build_messages(self, system: str, messages: list[dict]) -> list[dict]:
        result = [{"role": "system", "content": system}]
        for message in messages:
            role = message["role"]
            if role == "user":
                result.append({"role": "user", "content": message["content"]})
            elif role == "assistant":
                item = {"role": "assistant", "content": message.get("content") or ""}
                calls = message.get("tool_calls") or []
                if calls:
                    item["tool_calls"] = [{"function": {"name": c.name, "arguments": c.arguments}} for c in calls]
                result.append(item)
            elif role == "tool":
                result.append({"role": "tool", "content": message["content"], "tool_name": message.get("name", "")})
        return result

    @staticmethod
    def build_tools(tools: list[dict]) -> list[dict]:
        return [{"type": "function", "function": t} for t in tools]

    # ─── ошибки ───
    def _connection_error(self) -> LLMError:
        return LLMError(
            f"Ollama не запущена или недоступна по адресу {self.host}.",
            f"Запустите приложение Ollama или выполните в терминале: ollama serve. "
            f"Затем скачайте модель: ollama pull {self.model}. {_DOWNLOAD_HINT}",
        )

    def _response_error(self, exc) -> LLMError:
        text = str(getattr(exc, "error", exc))
        low = text.lower()
        status = getattr(exc, "status_code", -1)
        if "does not support tools" in low:
            return LLMError(f"Модель «{self.model}» не поддерживает инструменты (tools).",
                            "Выберите модель с поддержкой инструментов, например: ollama pull qwen3:8b "
                            "и укажите её в config.yaml → ollama.model")
        if status == 404 or ("not found" in low and "model" in low):
            return LLMError(f"Модель «{self.model}» не найдена в Ollama.",
                            f"Скачайте её командой: ollama pull {self.model}")
        if "memory" in low:
            return LLMError(f"Не хватает памяти для модели «{self.model}»: {text}",
                            "Закройте тяжёлые программы или выберите модель поменьше, например qwen3:4b-instruct "
                            "(ollama pull qwen3:4b-instruct и ollama.model в config.yaml).")
        return LLMError(f"Ошибка Ollama: {text}")

    # ─── размышления ───
    def _inspect_thinking(self, info=None) -> None:
        """Умеет ли модель отвечать без размышлений. «Думающие» модели (qwen3:4b, deepseek-r1…) размышляют
        всегда: им передаём think=True, чтобы Ollama отделила рассуждения от ответа — иначе они попадут
        в чат и в озвучку («Хорошо, пользователь просит…»)."""
        self._thinking_checked_for = self.model
        values = None
        try:  # в /api/show новых версий Ollama есть thinking.values: [True] — выключить нельзя
            response = self.client._client.post("/api/show", json={"model": self.model})
            if response.status_code == 200:
                values = (response.json().get("thinking") or {}).get("values")
        except Exception:
            values = None
        if isinstance(values, list) and values:
            self._always_thinks = values == [True]
            return
        try:
            info = info if info is not None else self.client.show(self.model)
            capabilities = getattr(info, "capabilities", None) or []
            self._always_thinks = (not capabilities or "thinking" in capabilities) and \
                template_always_thinks(getattr(info, "template", "") or "")
        except Exception:
            self._always_thinks = False

    @property
    def always_thinks(self) -> bool:
        if self._thinking_checked_for != self.model:
            self._inspect_thinking()
        return self._always_thinks

    def _think_value(self) -> bool:
        return bool(self.config.get("ollama.think", False)) or self.always_thinks

    # ─── запросы ───
    def check(self) -> str | None:
        try:
            info = self.client.show(self.model)
        except self._ollama.ResponseError as exc:
            raise self._response_error(exc) from None
        except (ConnectionError, httpx.TransportError):
            raise self._connection_error() from None
        capabilities = getattr(info, "capabilities", None) or []
        if capabilities and "tools" not in capabilities:
            raise LLMError(f"Модель «{self.model}» не умеет вызывать инструменты — управлять ПК она не сможет.",
                           "Установите модель с поддержкой tools: ollama pull qwen3:8b")
        self._inspect_thinking(info)
        if self._always_thinks and not self.config.get("ollama.think", False):
            return (f"Модель {self.model} всегда сначала размышляет, поэтому отвечает медленно. Для быстрых "
                    f"ответов скачайте модель без размышлений ({FAST_MODELS_HINT}): например, "
                    "ollama pull qwen3:4b-instruct — и выберите её в «Настройки → Модели».")
        return None

    # ─── зрение ───
    def _vision_model(self) -> str:
        model = str(self.config.get("ollama.vision_model") or "").strip()
        if model:
            return model
        if self._main_has_vision is None:
            try:
                capabilities = getattr(self.client.show(self.model), "capabilities", None) or []
                self._main_has_vision = "vision" in capabilities
            except Exception:
                self._main_has_vision = False
        return self.model if self._main_has_vision else ""

    def supports_vision(self) -> bool:
        return bool(self._vision_model())

    def vision(self, prompt: str, image: bytes, mime: str = "image/jpeg") -> str:
        model = self._vision_model()
        if not model:
            raise LLMError("У локальной модели нет зрения.",
                           "Скачайте модель со зрением: ollama pull qwen2.5vl:7b — и укажите её в config.yaml → "
                           "ollama.vision_model.")
        # Отдельную модель со зрением долго в памяти не держим: рядом с основной ей может не хватить места.
        keep_alive = self.config.get("ollama.keep_alive", "2h") if model == self.model else "5m"
        try:
            response = self.client.chat(model=model, messages=[{"role": "user", "content": prompt, "images": [image]}],
                                        options={"temperature": 0.2, "num_ctx": int(self.config.get("ollama.num_ctx", 16384))},
                                        keep_alive=keep_alive)
        except self._ollama.ResponseError as exc:
            raise self._response_error(exc) from None
        except (ConnectionError, httpx.TransportError):
            raise self._connection_error() from None
        return strip_think(response.message.content or "")

    def _options(self) -> dict:
        return {
            "temperature": float(self.config.get("ollama.temperature", 0.4)),
            "num_ctx": int(self.config.get("ollama.num_ctx", 8192)),
        }

    def warmup(self, system: str, tools: list[dict]) -> None:
        """Прогоняет системный промпт с описаниями инструментов: Ollama кэширует этот
        общий префикс, и первый настоящий ответ приходит заметно быстрее."""
        if not self.config.get("ollama.preload", True):
            return
        kwargs = {
            "model": self.model,
            "messages": self.build_messages(system, [{"role": "user", "content": "Проверка связи."}]),
            "tools": self.build_tools(tools) if tools else None,
            "options": {**self._options(), "num_predict": 1},
            "keep_alive": self.config.get("ollama.keep_alive", "2h"),
        }
        if self._think_supported:
            kwargs["think"] = self._think_value()
        try:
            self.client.chat(**kwargs)
        except Exception as exc:
            log.info("Прогрев модели не удался (не страшно): %s", exc)

    def stream_chat(self, system: str, messages: list[dict], tools: list[dict], allow_tools: bool,
                    cancel: threading.Event, on_text: TextCallback) -> AssistantTurn:
        try:
            return self._stream(system, messages, tools, allow_tools, cancel, on_text)
        except self._ollama.ResponseError as exc:
            if "think" in str(exc).lower() and self._think_supported:
                log.info("Сервер Ollama не поддерживает параметр think — повторяю без него")
                self._think_supported = False
                return self._stream(system, messages, tools, allow_tools, cancel, on_text)
            raise self._response_error(exc) from None
        except (ConnectionError, httpx.ConnectError, httpx.ConnectTimeout):
            raise self._connection_error() from None
        except httpx.TimeoutException:
            raise LLMError(f"Ollama не ответила за {self.config.get('llm.timeout', 300)} с.",
                           "Модель может ещё загружаться в память — повторите запрос или увеличьте llm.timeout.") from None
        except httpx.HTTPError as exc:
            raise LLMError(f"Ошибка соединения с Ollama: {exc}") from None

    def _stream(self, system, messages, tools, allow_tools, cancel, on_text) -> AssistantTurn:
        kwargs = {
            "model": self.model,
            "messages": self.build_messages(system, messages),
            "stream": True,
            "options": self._options(),
            "keep_alive": self.config.get("ollama.keep_alive", "2h"),
        }
        if allow_tools and tools:
            kwargs["tools"] = self.build_tools(tools)
        if self._think_supported:
            kwargs["think"] = self._think_value()  # рассуждения придут отдельно (message.thinking) — их не показываем

        stream = self.client.chat(**kwargs)
        # Если сервер не понимает think, рассуждения «думающей» модели придут в тексте до </think>.
        think_filter = ThinkFilter(inside=self.always_thinks and not self._think_supported)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        # Пока начало ответа похоже на JSON, текст придерживаем: это может быть вызов
        # инструмента, записанный текстом, — его нельзя показывать и озвучивать.
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

        try:
            for chunk in stream:
                if cancel.is_set():
                    break
                message = chunk.message
                if message.content:
                    visible = think_filter.feed(message.content)
                    if visible:
                        emit(visible)
                for call in message.tool_calls or []:
                    calls.append(ToolCall(name=call.function.name, arguments=dict(call.function.arguments or {})))
        finally:
            close = getattr(stream, "close", None)
            if close:
                close()
        rest = think_filter.flush()
        if rest and not cancel.is_set():
            emit(rest)
        if think_filter.stray_close and not self._always_thinks:
            log.warning("Модель %s размышляет, хотя её просили не делать этого, — дальше рассуждения отделяются",
                        self.model)
            self._always_thinks = True
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
            self.client._client.close()
        except Exception:
            pass
