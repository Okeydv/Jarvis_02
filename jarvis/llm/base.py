"""Общий интерфейс языковых моделей.

История диалога хранится в нейтральном формате:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [ToolCall], "raw": {"backend": имя, "data": ...}}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
Каждый бэкенд сам переводит её в формат своего API. Поле raw хранит «родной» ответ
модели (например, подписи размышлений Gemini), чтобы вернуть его API без потерь.
"""

from __future__ import annotations

import threading
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


class LLMError(Exception):
    """Ошибка, понятная пользователю: что случилось и как исправить."""

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return f"{self.message} {self.hint}".strip()


@dataclass
class ToolCall:
    name: str
    arguments: dict
    id: str = field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")


@dataclass
class AssistantTurn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None


TextCallback = Callable[[str], None]


class LLMBackend(ABC):
    name = "base"
    title = "Base"

    def __init__(self, config):
        self.config = config

    @property
    @abstractmethod
    def model(self) -> str:
        ...

    @abstractmethod
    def stream_chat(self, system: str, messages: list[dict], tools: list[dict], allow_tools: bool,
                    cancel: threading.Event, on_text: TextCallback) -> AssistantTurn:
        """Один шаг диалога. Текст ответа отдаётся по мере генерации через on_text.

        tools — список {"name", "description", "parameters"}; allow_tools=False требует
        ответить только текстом (последний шаг агентского цикла).
        """

    def check(self) -> str | None:
        """Быстрая проверка доступности. Возвращает сообщение для чата или None."""
        return None

    def warmup(self, system: str, tools: list[dict]) -> None:  # noqa: B027 — необязательный шаг
        """Подготовка к первому запросу (по умолчанию ничего не делает)."""

    def close(self) -> None:  # noqa: B027 — по умолчанию освобождать нечего
        """Освобождает сетевые соединения бэкенда."""

    def raw_for(self, message: dict) -> Any:
        """Родной ответ модели этого же бэкенда (если сообщение создано им)."""
        raw = message.get("raw")
        if isinstance(raw, dict) and raw.get("backend") == self.name:
            return raw.get("data")
        return None


def tool_calls_as_text(message: dict) -> str:
    """Вызовы инструментов чужого бэкенда в виде текста (для смены модели посреди диалога)."""
    import json

    lines = [message.get("content") or ""]
    for call in message.get("tool_calls") or []:
        lines.append(f"[Вызван инструмент {call.name} с аргументами {json.dumps(call.arguments, ensure_ascii=False)}]")
    return "\n".join(line for line in lines if line).strip()


def pair_tool_results(messages: list[dict]) -> dict[str, dict]:
    """tool_call_id → сообщение с результатом."""
    return {m["tool_call_id"]: m for m in messages if m.get("role") == "tool" and m.get("tool_call_id")}
