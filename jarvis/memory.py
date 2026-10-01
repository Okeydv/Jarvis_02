"""Долгая память: факты о пользователе («запомни, что я живу в Казани») и история диалога,
которая переживает перезапуск программы."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .llm.base import ToolCall
from .storage import JsonFile
from .text_utils import normalize, similarity

MAX_FACTS = 60
MAX_PROMPT_CHARS = 3000
_FORGET_ALL = {"все", "всё", "всё что знаешь", "все факты", "всю память"}


class Memory:
    def __init__(self, path: Path):
        self._file = JsonFile(path, {"facts": []})

    def facts(self) -> list[str]:
        return [str(item.get("text", "")) for item in self._file.load().get("facts", []) if item.get("text")]

    def remember(self, text: str) -> str:
        text = " ".join(text.split()).strip().rstrip(".")
        if not text:
            return "Нечего запоминать."
        with self._file.lock:
            data = self._file.load()
            facts = data.setdefault("facts", [])
            key = normalize(text)
            for item in facts:
                old = normalize(item.get("text", ""))
                if old == key:
                    return f"Я это уже помню: {text}."
                if similarity(old, key) >= 0.8:  # уточнение того же факта — заменяем
                    previous = item["text"]
                    item.update(text=text, added=datetime.now().isoformat(timespec="minutes"))
                    self._file.save(data)
                    return f"Обновил: было «{previous}», теперь «{text}»."
            facts.append({"text": text, "added": datetime.now().isoformat(timespec="minutes")})
            del facts[:-MAX_FACTS]
            self._file.save(data)
        return f"Запомнил: {text}."

    def forget(self, query: str) -> list[str]:
        key = normalize(query)
        with self._file.lock:
            data = self._file.load()
            facts = data.get("facts", [])
            if key in _FORGET_ALL:
                removed = [f["text"] for f in facts]
                data["facts"] = []
            else:
                removed = [f["text"] for f in facts
                           if key and (key in normalize(f["text"]) or similarity(key, normalize(f["text"])) >= 0.6)]
                data["facts"] = [f for f in facts if f["text"] not in removed]
            if removed:
                self._file.save(data)
        return removed

    def set_all(self, facts: list[str]) -> None:
        """Список фактов целиком (правка в окне «Настройки»)."""
        stamp = datetime.now().isoformat(timespec="minutes")
        clean = [" ".join(f.split()).strip() for f in facts if f.strip()]
        self._file.save({"facts": [{"text": f, "added": stamp} for f in clean[-MAX_FACTS:]]})

    def prompt_block(self) -> str:
        facts = self.facts()
        if not facts:
            return ""
        lines, size = [], 0
        for fact in reversed(facts):  # свежие факты важнее
            size += len(fact) + 3
            if size > MAX_PROMPT_CHARS:
                break
            lines.append(f"- {fact}")
        return "Что ты знаешь о пользователе (учитывай, когда это к месту):\n" + "\n".join(reversed(lines))


class HistoryStore:
    """Последние сообщения диалога в файле: после перезапуска разговор продолжается."""

    def __init__(self, path: Path, limit: int = 60):
        self._file = JsonFile(path, {"messages": []})
        self.limit = limit

    @staticmethod
    def _dump(message: dict) -> dict:
        item = {"role": message["role"], "content": message.get("content") or ""}
        if message.get("tool_calls"):
            item["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in message["tool_calls"]]
        for key in ("tool_call_id", "name"):
            if message.get(key):
                item[key] = message[key]
        return item  # «сырые» ответы моделей (raw) не сохраняем — они не переживут перезапуск

    @staticmethod
    def _restore(item: dict) -> dict | None:
        role = item.get("role")
        if role not in ("user", "assistant", "tool"):
            return None
        message = {"role": role, "content": str(item.get("content") or "")}
        if item.get("tool_calls"):
            message["tool_calls"] = [ToolCall(name=c.get("name", ""), arguments=c.get("arguments") or {},
                                              id=c.get("id") or ToolCall(name="", arguments={}).id)
                                     for c in item["tool_calls"] if isinstance(c, dict)]
        for key in ("tool_call_id", "name"):
            if item.get(key):
                message[key] = item[key]
        return message

    def save(self, history: list[dict]) -> None:
        window = history[-self.limit:]
        while window and window[0]["role"] != "user":  # окно начинается с реплики пользователя
            window = window[1:]
        self._file.save({"saved": datetime.now().isoformat(timespec="seconds"),
                         "messages": [self._dump(m) for m in window]})

    def load(self) -> list[dict]:
        messages = [self._restore(item) for item in self._file.load().get("messages", []) if isinstance(item, dict)]
        return [m for m in messages if m is not None]

    def clear(self) -> None:
        self._file.save({"messages": []})
