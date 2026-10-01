"""Данные Джарвиса между запусками: память, история диалога, сценарии, расписание, навыки,
резервные копии файлов. По умолчанию всё лежит в «Документы\\Jarvis» (рядом с заметками и кодом),
поэтому не пропадает при обновлении программы."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from . import winapi

log = logging.getLogger(__name__)


def data_folder(config) -> Path:
    custom = str(config.get("paths.data", "") or "").strip()
    if custom:
        return Path(os.path.expandvars(os.path.expanduser(custom)))
    return winapi.known_folder("Documents") / "Jarvis"


class JsonFile:
    """JSON-файл с атомарной записью: при сбое посреди записи старые данные не портятся."""

    def __init__(self, path: Path, default: Any):
        self.path = Path(path)
        self.default = default
        self.lock = threading.RLock()

    def load(self) -> Any:
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            except FileNotFoundError:
                return copy.deepcopy(self.default)
            except (OSError, ValueError) as exc:
                log.warning("Не удалось прочитать %s: %s — начинаю с чистого листа", self.path, exc)
                return copy.deepcopy(self.default)
            return data if isinstance(data, type(self.default)) else copy.deepcopy(self.default)

    def save(self, data: Any) -> None:
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temp = self.path.with_suffix(self.path.suffix + ".tmp")
                temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                os.replace(temp, self.path)
            except OSError as exc:  # запись данных не должна ронять программу
                log.warning("Не удалось сохранить %s: %s", self.path, exc)
