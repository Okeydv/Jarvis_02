"""Загрузка настроек из config.yaml и ключей из .env."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
ENV_PATH = ROOT_DIR / ".env"
# Всё, что меняется в окне программы, сохраняется сюда (а config.yaml остаётся нетронутым,
# поэтому обновление через git pull не конфликтует с вашими настройками).
USER_SETTINGS_PATH = ROOT_DIR / "user_settings.json"

BACKENDS = ("ollama", "gigachat", "gemini", "qwen")

DEFAULT_SYSTEM_PROMPT = (
    "Ты — Джарвис, ИИ-ассистент, управляющий компьютером пользователя. Отвечай по-русски, "
    "коротко, обращайся “сэр”, лёгкая британская ирония. Для действий с ПК используй "
    "инструменты; не говори, что действие выполнено, если не вызвал инструмент.\n"
    "У тебя есть доступ к этому компьютеру через инструменты: программы и окна, сайты и поиск, "
    "папки и файлы, громкость и яркость, музыка, ввод текста и сочетания клавиш, буфер обмена, "
    "скриншоты, таймеры и напоминания, заметки, погода, калькулятор, параметры Windows, блокировка, "
    "сон и выключение. Если просьбу можно выполнить инструментом — сразу вызывай его, а не объясняй, "
    "как сделать вручную. Никогда не говори, что у тебя нет доступа к компьютеру. Считай только "
    "инструментом calculate.\n"
    "Ты умеешь программировать. Если просят написать программу, скрипт, сайт или игру — сохрани код "
    "в файл инструментом write_file (он откроется в редакторе) и коротко скажи, что сделал; код вслух "
    "не зачитывай. Запустить программу на Python — run_python (пользователь подтверждает запуск; "
    "программы с окном и игры — с wait=false).\n"
    "Пользователь обычно говорит голосом: его речь распознаётся и приходит тебе текстом, возможны "
    "ошибки распознавания — угадывай смысл. Ответ будет озвучен, поэтому пиши обычным текстом без "
    "markdown, списков и эмодзи.\n"
    "Если инструмент вернул ошибку или пользователь отменил действие — честно скажи об этом. "
    "Отвечай одним-двумя предложениями, если не просят подробнее."
)

# Значения по умолчанию: используются, если в config.yaml чего-то нет.
DEFAULTS: dict[str, Any] = {
    "llm": {"backend": "ollama", "max_steps": 5, "history_messages": 20, "timeout": 300, "fast_replies": True},
    "ollama": {
        "host": "http://localhost:11434",
        "model": "qwen3:8b",
        "think": False,
        "temperature": 0.4,
        "keep_alive": "30m",
        "num_ctx": 12288,
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
    "qwen": {
        "base_url": "https://api.bazaarlink.ai/v1",
        "model": "qwen/qwen3.7-flash:free",
        "temperature": 0.4,
        "thinking": False,
        "free_fallback": False,
        "proxy": "",
    },
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "voice": {
        "vosk_model_path": "models/vosk-model-small-ru-0.22",
        "vosk_model_url": "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip",
        "input_device": None,
        "input_gain": 1.0,
        "jarvis_mode": True,
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
        "allow_code": True,
        "code_timeout": 60,
        "code_folder": "",
        "code_editor": "",
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

    def __init__(self, data: dict, path: Path | None = None, errors: list[str] | None = None,
                 user_path: Path | None = None, user_data: dict | None = None, env_path: Path = ENV_PATH):
        self.data = data
        self.path = path
        self.env_path = env_path
        self.user_path = user_path
        self.user_data: dict = user_data or {}
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
        """Меняет значение в памяти и (persist=True) запоминает его в user_settings.json."""
        with self._lock:
            parts = dotted.split(".")
            _assign(self.data, parts, value)
            if persist and self.user_path is not None:
                _assign(self.user_data, parts, value)
                try:
                    tmp = self.user_path.with_suffix(".tmp")
                    tmp.write_text(json.dumps(self.user_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    os.replace(tmp, self.user_path)
                except OSError as exc:  # запись настроек не должна ронять программу
                    log.warning("Не удалось сохранить %s в %s: %s", dotted, self.user_path, exc)

    def reload_env(self) -> None:
        """Перечитывает .env — ключ можно добавить, не перезапуская программу."""
        _load_env(self.env_path, override=True)

    def save_secret(self, name: str, value: str) -> None:
        """Записывает ключ API в .env (файл создаётся при необходимости) и сразу применяет его."""
        from dotenv import set_key

        value = value.strip()
        with self._lock:
            if not self.env_path.exists():
                self.env_path.write_text("", encoding="utf-8")
            set_key(str(self.env_path), name, value)
        os.environ[name] = value

    def resolve_path(self, value: str | os.PathLike | None) -> Path | None:
        """Относительные пути считаются от папки программы; %VAR% и ~ раскрываются."""
        if not value:
            return None
        path = Path(os.path.expanduser(os.path.expandvars(str(value))))
        return path if path.is_absolute() else ROOT_DIR / path


def mask_secret(value: str | None) -> str:
    """«sk-bl-abcdef123456» → «sk-b…3456» — чтобы показать, что ключ задан, не раскрывая его."""
    value = (value or "").strip()
    if not value:
        return ""
    return f"{value[:4]}…{value[-4:]}" if len(value) > 12 else "••••"


def load_config(path: Path = CONFIG_PATH, env_path: Path = ENV_PATH,
                user_path: Path | None = USER_SETTINGS_PATH) -> Config:
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

    user_data: dict = {}
    if user_path is not None and user_path.exists():
        try:
            user_data = json.loads(user_path.read_text(encoding="utf-8-sig")) or {}
            if not isinstance(user_data, dict):
                user_data = {}
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"Не удалось прочитать {user_path.name}: {exc}. Настройки окна сброшены.")
            user_data = {}

    data = deep_merge(deep_merge(DEFAULTS, raw), user_data)
    backend = str(data["llm"].get("backend", "ollama")).strip().lower()
    if backend not in BACKENDS:
        errors.append(f"Неизвестный бэкенд «{backend}» в llm.backend — выбран ollama.")
        backend = "ollama"
    data["llm"]["backend"] = backend
    return Config(data, path if path.exists() else None, errors, user_path, user_data, env_path)


def _assign(tree: dict, parts: list[str], value: Any) -> None:
    node = tree
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def _load_env(env_path: Path, override: bool = False) -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv есть в requirements
        return
    if env_path.exists():
        load_dotenv(env_path, override=override, encoding="utf-8-sig")
