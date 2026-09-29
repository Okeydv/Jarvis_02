"""Агентский цикл: модель → инструменты → результаты модели → финальный ответ."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Protocol

from .config import DEFAULT_SYSTEM_PROMPT
from .llm.base import LLMBackend, LLMError, ToolCall
from .tools import ToolRegistry, ToolResult

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

        for step in range(max_steps):
            if cancel.is_set():
                break
            allow_tools = step < max_steps - 1  # последний шаг — только текстовый ответ
            self.events.on_state("thinking")
            with self._lock:
                window = trim_history(self.history, limit)
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
            for call in calls:
                if cancel.is_set():
                    result = ToolResult("cancelled", "Отменено пользователем.")
                    self.events.on_tool_result(call, result)
                else:
                    self.events.on_tool_start(call)
                    result = self.registry.execute(call.name, call.arguments, cancel)
                    self.events.on_tool_result(call, result)
                self._append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": result.text})
                outcome.tools_used += 1
                if not result.ok:
                    outcome.tool_failures += 1

        outcome.cancelled = cancel.is_set()
        self._truncate()
        return outcome
