"""Резервные копии файлов, которые меняет Джарвис: любую правку можно откатить одной командой."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .storage import JsonFile

MAX_ENTRIES = 200


class Backups:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._index = JsonFile(self.folder / "index.json", {"entries": []})

    def backup(self, path: Path, reason: str = "") -> Path | None:
        """Копия файла перед изменением (если файл существует)."""
        path = Path(path)
        if not path.is_file():
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        copy = self.folder / f"{datetime.now():%Y%m%d-%H%M%S-%f}__{path.name}"
        shutil.copy2(path, copy)
        with self._index.lock:
            data = self._index.load()
            entries = data.setdefault("entries", [])
            entries.append({"original": str(path), "copy": str(copy), "time": datetime.now().isoformat(timespec="seconds"),
                            "reason": reason})
            for old in entries[:-MAX_ENTRIES]:
                Path(old["copy"]).unlink(missing_ok=True)
            del entries[:-MAX_ENTRIES]
            self._index.save(data)
        return copy

    def entries(self) -> list[dict]:
        return [e for e in self._index.load().get("entries", []) if Path(e.get("copy", "")).is_file()]

    def latest(self, path: str = "") -> dict | None:
        entries = self.entries()
        if path:
            target = str(Path(path))
            name = Path(path).name.lower()
            entries = [e for e in entries if e["original"] == target] or \
                      [e for e in entries if Path(e["original"]).name.lower() == name]
        return entries[-1] if entries else None

    def restore(self, path: str = "") -> tuple[Path, str]:
        """Возвращает файл к последней копии. Текущая версия тоже сохраняется — откат можно отменить."""
        entry = self.latest(path)
        if entry is None:
            raise FileNotFoundError(f"нет резервных копий{' для ' + path if path else ''}")
        original, copy = Path(entry["original"]), Path(entry["copy"])
        self.backup(original, reason="перед откатом")
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copy, original)
        with self._index.lock:
            data = self._index.load()
            data["entries"] = [e for e in data.get("entries", []) if e.get("copy") != entry["copy"]]
            self._index.save(data)
        copy.unlink(missing_ok=True)
        return original, entry["time"]
