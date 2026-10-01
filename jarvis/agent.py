"""Агентский цикл: модель → инструменты → результаты модели → финальный ответ."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

import re

from .config import DEFAULT_SYSTEM_PROMPT
from .llm.base import LLMBackend, LLMError, ToolCall
from .text_utils import normalize
from .tools import TOOLS, ToolRegistry, ToolResult

log = logging.getLogger(__name__)

MAX_STORED_MESSAGES = 200


class AgentEvents(Protocol):
    def on_state(self, state: str) -> None: ...
    def on_text(self, delta: str) -> None: ...
    def on_turn_end(self) -> None: ...
    def on_tool_start(self, call: ToolCall) -> None: ...
    def on_tool_result(self, call: ToolCall, result: ToolResult) -> None: ...
    def on_error(self, message: str, hint: str = "") -> None: ...


@dataclass
class AgentOutcome:
    text: str = ""
    tools_used: int = 0
    tool_failures: int = 0
    cancelled: bool = False
    failed: bool = False
    fast: bool = False  # ответ дан сразу по результату действия, без второго запроса к модели


# Признаки просьбы из нескольких шагов: «открой блокнот и напиши…», «…, потом…»
_MULTI_STEP_RE = re.compile(r"(^|\s)(и|потом|затем|после|также|ещё|еще|а)(\s|$)")


def is_single_action(text: str) -> bool:
    """Похоже ли сказанное на одно действие («открой браузер», «громче»)."""
    if "," in text or ";" in text:
        return False
    return not _MULTI_STEP_RE.search(normalize(text))


def _speakable(text: str) -> str:
    """Убирает технические подробности в скобках: «(notepad.exe)», «(https://…)»."""
    return re.sub(r"\s*\([^()]*[A-Za-z\\/][^()]*\)", "", text).strip()


def quick_reply(results: list[ToolResult], said_something: bool) -> str:
    parts: list[str] = []
    for result in results:
        text = "Как скажете, сэр, отменяю." if result.status == "cancelled" else _speakable(result.text)
        if text and text not in parts:
            parts.append(text)
    reply = " ".join(parts)
    if not said_something and all(r.ok for r in results):
        reply = f"Готово, сэр. {reply}".strip()
    return reply


_SHORT_TOOL_RESULT = 600
_SHORT_ARGUMENT = 400


def compact_history(window: list[dict]) -> list[dict]:
    """Старые длинные результаты инструментов и аргументы (например, код в write_file) сокращаются:
    запрос к модели становится короче, а ответ — быстрее. Текущий запрос не трогаем."""
    last_user = max((i for i, m in enumerate(window) if m["role"] == "user"), default=0)
    result = []
    for index, message in enumerate(window):
        if index >= last_user:
            result.append(message)
            continue
        if message["role"] == "tool" and len(message.get("content") or "") > _SHORT_TOOL_RESULT:
            content = message["content"]
            message = {**message, "content": content[:_SHORT_TOOL_RESULT - 100] + f"…(сокращено, всего {len(content)} символов)"}
        elif message["role"] == "assistant" and message.get("tool_calls"):
            calls = []
            changed = False
            for call in message["tool_calls"]:
                arguments = {}
                for key, value in call.arguments.items():
                    if isinstance(value, str) and len(value) > _SHORT_ARGUMENT:
                        value = value[:_SHORT_ARGUMENT - 100] + f"…(сокращено, всего {len(value)} символов)"
                        changed = True
                    arguments[key] = value
                calls.append(ToolCall(name=call.name, arguments=arguments, id=call.id))
            if changed:
                message = {**message, "tool_calls": calls}
        result.append(message)
    return result


def trim_history(history: list[dict], limit: int) -> list[dict]:
    """Последние limit сообщений; окно всегда начинается с реплики пользователя
    и включает текущий запрос целиком."""
    if not history:
        return []
    last_user = max((i for i, m in enumerate(history) if m["role"] == "user"), default=0)
    start = min(max(0, len(history) - limit), last_user)
    window = history[start:]
    while window and window[0]["role"] != "user":
        window = window[1:]
    return window


class Agent:
    def __init__(self, config, registry: ToolRegistry, get_backend: Callable[[], LLMBackend], events: AgentEvents):
        self.config = config
        self.registry = registry
        self.get_backend = get_backend
        self.events = events
        self.history: list[dict] = []
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self.history.clear()

    def _append(self, message: dict) -> None:
        with self._lock:
            self.history.append(message)

    def _rollback(self, length: int) -> None:
        """Убирает запрос, на который модель так и не ответила (чтобы не дублировать его)."""
        with self._lock:
            del self.history[length - 1:]

    def _truncate(self) -> None:
        with self._lock:
            if len(self.history) > MAX_STORED_MESSAGES:
                self.history[:] = trim_history(self.history, MAX_STORED_MESSAGES)

    def run(self, user_text: str, cancel: threading.Event) -> AgentOutcome:
        max_steps = max(1, int(self.config.get("llm.max_steps", 5)))
        limit = max(2, int(self.config.get("llm.history_messages", 20)))
        system = str(self.config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT).strip()
        outcome = AgentOutcome()

        self._append({"role": "user", "content": user_text})
        with self._lock:
            start_length = len(self.history)

        try:
            backend = self.get_backend()
        except LLMError as exc:
            self._rollback(start_length)
            self.events.on_error(exc.message, exc.hint)
            outcome.failed = True
            return outcome
        tools = self.registry.schemas()
        if any(t["name"] == "run_powershell" for t in tools):
            system += ("\nДля задач на компьютере, для которых нет отдельного инструмента, используй "
                       "run_powershell — пользователь подтверждает каждую команду.")

        for step in range(max_steps):
            if cancel.is_set():
                break
            allow_tools = step < max_steps - 1  # последний шаг — только текстовый ответ
            self.events.on_state("thinking")
            with self._lock:
                window = compact_history(trim_history(self.history, limit))
            try:
                turn = backend.stream_chat(system, window, tools, allow_tools, cancel, self.events.on_text)
            except LLMError as exc:
                self.events.on_turn_end()
                if not outcome.tools_used:
                    self._rollback(start_length)
                self.events.on_error(exc.message, exc.hint)
                outcome.failed = True
                return outcome
            except Exception as exc:  # неожиданный сбой библиотеки не должен ронять программу
                log.exception("Сбой при обращении к модели")
                self.events.on_turn_end()
                if not outcome.tools_used:
                    self._rollback(start_length)
                self.events.on_error(f"Непредвиденная ошибка модели: {type(exc).__name__}: {exc}")
                outcome.failed = True
                return outcome
            self.events.on_turn_end()

            if cancel.is_set():
                if turn.text:
                    self._append({"role": "assistant", "content": turn.text + " …"})
                break

            calls = turn.tool_calls if allow_tools else []
            if turn.text or calls:
                self._append({"role": "assistant", "content": turn.text, "tool_calls": calls, "raw": turn.raw})
            if not calls:
                outcome.text = turn.text
                break

            self.events.on_state("executing")
            results: list[ToolResult] = []
            for call in calls:
                if cancel.is_set():
                    result = ToolResult("cancelled", "Отменено пользователем.")
                    self.events.on_tool_result(call, result)
                else:
                    self.events.on_tool_start(call)
                    result = self.registry.execute(call.name, call.arguments, cancel)
                    self.events.on_tool_result(call, result)
                results.append(result)
                self._append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result.text})
                outcome.tools_used += 1
                if not result.ok:
                    outcome.tool_failures += 1

            if self._can_answer_now(user_text, calls, results, cancel):
                # Простое действие выполнено — результат и есть ответ: второй запрос к модели
                # занял бы ещё несколько секунд (на локальной модели — десятки).
                reply = quick_reply(results, said_something=bool(turn.text.strip()))
                self.events.on_text(reply)
                self.events.on_turn_end()
                self._append({"role": "assistant", "content": reply})
                outcome.text, outcome.fast = reply, True
                break

        outcome.cancelled = cancel.is_set()
        self._truncate()
        return outcome

    def _can_answer_now(self, user_text: str, calls: list[ToolCall], results: list[ToolResult],
                        cancel: threading.Event) -> bool:
        if cancel.is_set() or not self.config.get("llm.fast_replies", True) or not is_single_action(user_text):
            return False
        if any(r.status == "error" for r in results):
            return False  # ошибку модель объяснит и, может быть, исправит
        for call in calls:
            tool = TOOLS.get(call.name)
            if tool is None or not tool.is_quick(call.arguments if isinstance(call.arguments, dict) else {}):
                return False
        return True
