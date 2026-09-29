"""Загрузка настроек из config.yaml и ключей из .env."""

from __future__ import annotations

import copy
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
ENV_PATH = ROOT_DIR / ".env"

BACKENDS = ("ollama", "gigachat", "gemini")

DEFAULT_SYSTEM_PROMPT = (
    "Ты — Джарвис, ИИ-ассистент, управляющий компьютером пользователя. Отвечай по-русски, "
    "коротко, обращайся “сэр”, лёгкая британская ирония. Для действий с ПК используй "
    "инструменты; не говори, что действие выполнено, если не вызвал инструмент."
)

# Значения по умолчанию: используются, если в config.yaml чего-то нет.
DEFAULTS: dict[str, Any] = {
    "llm": {"backend": "ollama", "max_steps": 5, "history_messages": 20, "timeout": 300},
    "ollama": {
        "host": "http://localhost:11434",
        "model": "qwen3:8b",
        "think": False,
        "temperature": 0.4,
        "keep_alive": "30m",
        "num_ctx": 8192,
        "preload": True,
    },
    "gigachat": {
        "model": "GigaChat-2",
        "scope": "GIGACHAT_API_PERS",
        "temperature": 0.4,
        "ca_bundle_file": "certs/russian_trusted_root_ca.crt",
        "verify_ssl_certs": True,
    },
    "gemini": {
        "model": "gemini-flash-latest",
        "thinking_level": "low",
        "temperature": None,
        "proxy": "",
    },
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "voice": {
        "vosk_model_path": "models/vosk-model-small-ru-0.22",
        "input_device": None,
        "jarvis_mode": False,
        "speak_replies": True,
        "wake_words": ["джарвис", "джервис", "жарвис", "джарвиз"],
        "stop_words": ["стоп", "хватит", "замолчи", "замолкни", "остановись", "довольно"],
        "listen_timeout": 7,
        "max_phrase_seconds": 15,
        "follow_up_seconds": 6,
        "earcons": True,
    },
    "tts": {
        "speaker": "aidar",
        "sample_rate": 48000,
        "model_path": "models/silero/v4_ru.pt",
        "model_url": "https://models.silero.ai/models/tts/ru/v4_ru.pt",
        "threads": 4,
        "max_chunk_chars": 800,
        "volume": 1.0,
        "output_device": None,
    },
    "hotkey": "ctrl+alt+j",
    "tools": {
        "confirm_timeout": 20,
        "allow_powershell": False,
        "powershell_timeout": 30,
        "shutdown_delay": 10,
        "search_url": "https://www.google.com/search?q={query}",
        "apps": {},
    },
}


def deep_merge(base: dict, override: dict) -> dict:
    """Рекурсивно накладывает override на base (словари сливаются, остальное заменяется)."""
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


class Config:
    """Настройки с доступом по «точечному» пути: cfg.get("ollama.model")."""

    def __init__(self, data: dict, path: Path | None = None, errors: list[str] | None = None):
        self.data = data
        self.path = path
        self.load_errors = errors or []
        self._lock = threading.Lock()

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return default if node is None and default is not None else node

    def section(self, name: str) -> dict:
        value = self.data.get(name)
        return value if isinstance(value, dict) else {}

    def set(self, dotted: str, value: Any, persist: bool = False) -> None:
        """Меняет значение в памяти и (по желанию) в config.yaml, сохраняя комментарии."""
        with self._lock:
            parts = dotted.split(".")
            node = self.data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = value
            if persist and self.path is not None:
                try:
                    persist_scalar(self.path, parts, value)
                except Exception as exc:  # запись настроек не должна ронять программу
                    log.warning("Не удалось сохранить %s в %s: %s", dotted, self.path, exc)

    def reload_env(self) -> None:
        """Перечитывает .env — ключ можно добавить, не перезапуская программу."""
        _load_env(ENV_PATH, override=True)

    def resolve_path(self, value: str | os.PathLike | None) -> Path | None:
        """Относительные пути считаются от папки программы; %VAR% и ~ раскрываются."""
        if not value:
            return None
        path = Path(os.path.expanduser(os.path.expandvars(str(value))))
        return path if path.is_absolute() else ROOT_DIR / path


def load_config(path: Path = CONFIG_PATH, env_path: Path = ENV_PATH) -> Config:
    errors: list[str] = []
    _load_env(env_path)
    raw: dict = {}
    if path.exists():
        try:
            # utf-8-sig: Блокнот Windows может сохранить файл с BOM.
            raw = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
            if not isinstance(raw, dict):
                errors.append(f"{path.name}: ожидался словарь настроек, использую значения по умолчанию.")
                raw = {}
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            where = f" (строка {mark.line + 1})" if mark is not None else ""
            errors.append(f"Ошибка в {path.name}{where}: {exc}. Использую настройки по умолчанию.")
            raw = {}
    else:
        errors.append(f"Файл настроек {path} не найден — использую значения по умолчанию.")

    data = deep_merge(DEFAULTS, raw)
    backend = str(data["llm"].get("backend", "ollama")).strip().lower()
    if backend not in BACKENDS:
        errors.append(f"Неизвестный бэкенд «{backend}» в llm.backend — выбран ollama.")
        backend = "ollama"
    data["llm"]["backend"] = backend
    return Config(data, path if path.exists() else None, errors)


def _load_env(env_path: Path, override: bool = False) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv есть в requirements
        return
    if env_path.exists():
        load_dotenv(env_path, override=override, encoding="utf-8-sig")


# ─── Точечная запись значения в YAML без потери комментариев ───────────────

_KEY_RE = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^\s:#'"][^:#]*?)\s*:(?=\s|$)""")
_VALUE_RE = re.compile(r"^(?P<head>\s*[^:#]+?\s*:[ \t]*)(?P<value>[^#\r\n]*?)(?P<tail>[ \t]*(?:#.*)?)(?P<eol>\r?\n?)$")


def _find_key_line(lines: list[str], path: list[str]) -> int | None:
    stack: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        stripped = line.lstrip(" ")
        if not stripped.strip() or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        match = _KEY_RE.match(stripped)
        if not match:
            continue
        key = match.group("key").strip().strip("\"'")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if [k for _, k in stack] + [key] == path:
            return index
        stack.append((indent, key))
    return None


def _format_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if re.fullmatch(r"[\w.\-/]+", text) and text.lower() not in {"true", "false", "null", "yes", "no", "on", "off"}:
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def persist_scalar(path: Path, keys: list[str], value: Any) -> bool:
    """Заменяет значение ключа keys (например ["llm", "backend"]) прямо в тексте YAML."""
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines(keepends=True)
    index = _find_key_line(lines, keys)
    if index is None:
        log.info("Ключ %s не найден в %s — значение не сохранено", ".".join(keys), path.name)
        return False
    match = _VALUE_RE.match(lines[index])
    if not match:
        return False
    lines[index] = match.group("head") + _format_scalar(value) + match.group("tail") + match.group("eol")
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.replace(tmp, path)
    return True
