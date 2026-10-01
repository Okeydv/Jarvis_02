"""Бэкенды языковых моделей за общим интерфейсом LLMBackend."""

from __future__ import annotations

from .base import AssistantTurn, LLMBackend, LLMError, ToolCall

BACKEND_TITLES = {"ollama": "Ollama", "gigachat": "GigaChat", "gemini": "Gemini", "qwen": "Qwen"}


def create_backend(name: str, config) -> LLMBackend:
    """Создаёт бэкенд по имени. Библиотеки импортируются лениво — только нужная."""
    if name == "ollama":
        from .ollama_backend import OllamaBackend

        return OllamaBackend(config)
    if name == "gigachat":
        from .gigachat_backend import GigaChatBackend

        return GigaChatBackend(config)
    if name == "gemini":
        from .gemini_backend import GeminiBackend

        return GeminiBackend(config)
    if name == "qwen":
        from .qwen_backend import QwenBackend

        return QwenBackend(config)
    raise LLMError(f"Неизвестный бэкенд «{name}».", "Допустимые значения llm.backend: ollama, gigachat, gemini, qwen.")


__all__ = ["AssistantTurn", "BACKEND_TITLES", "LLMBackend", "LLMError", "ToolCall", "create_backend"]
