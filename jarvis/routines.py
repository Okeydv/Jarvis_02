"""Сценарии: последовательности действий, которые выполняются одной фразой и без модели
(«рабочий режим» = открыть почту, мессенджер, громкость 30 %), и расписание в духе cron
(«по будням в 9:00 — брифинг»)."""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .storage import JsonFile
from .text_utils import normalize, similarity

MAX_STEPS = 20
NOT_IN_ROUTINES = {"routine", "schedule"}  # сценарий не запускает сценарии — без бесконечных циклов
_RUN_PREFIXES = ("запусти сценарий", "выполни сценарий", "включи сценарий", "сценарий", "запусти", "включи",
                 "выполни", "активируй")


class RoutineError(ValueError):
    pass


def parse_steps(raw) -> list[dict]:
    """Шаги из JSON-строки или списка: [{"tool": "open_app", "arguments": {"name": "почта"}}, …]."""
    from .tools import TOOLS

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raise RoutineError("steps должен быть JSON-списком шагов [{\"tool\": …, \"arguments\": {…}}]") from None
    if isinstance(raw, dict):
        raw = raw.get("steps", [raw])
    if not isinstance(raw, list) or not raw:
        raise RoutineError("в сценарии нет шагов")
    if len(raw) > MAX_STEPS:
        raise RoutineError(f"слишком много шагов (больше {MAX_STEPS})")
    steps = []
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise RoutineError(f"шаг {index} должен быть объектом {{\"tool\": …, \"arguments\": …}}")
        name = str(item.get("tool") or item.get("name") or "").strip()
        arguments = item.get("arguments", item.get("args", {})) or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError:
                raise RoutineError(f"шаг {index}: arguments — не JSON") from None
        if name not in TOOLS:
            raise RoutineError(f"шаг {index}: инструмента «{name}» нет")
        if name in NOT_IN_ROUTINES:
            raise RoutineError(f"шаг {index}: в сценарий нельзя вложить {name}")
        if not isinstance(arguments, dict):
            raise RoutineError(f"шаг {index}: arguments должен быть объектом")
        steps.append({"tool": name, "arguments": arguments})
    return steps


class Routines:
    def __init__(self, path: Path):
        self._file = JsonFile(path, {"routines": {}})

    def all(self) -> dict[str, dict]:
        routines = self._file.load().get("routines", {})
        return {name: item for name, item in routines.items() if isinstance(item, dict) and item.get("steps")}

    def get(self, name: str) -> tuple[str, dict] | None:
        key = normalize(name)
        routines = self.all()
        for title, item in routines.items():
            if normalize(title) == key:
                return title, item
        best = max(routines.items(), key=lambda pair: similarity(key, normalize(pair[0])), default=None)
        if best and similarity(key, normalize(best[0])) >= 0.85:
            return best
        return None

    def save(self, name: str, steps) -> list[dict]:
        title = " ".join(name.split()).strip().strip("«»\"")
        if not title:
            raise RoutineError("у сценария должно быть название")
        parsed = parse_steps(steps)
        with self._file.lock:
            data = self._file.load()
            existing = self.get(title)
            if existing:
                data["routines"].pop(existing[0], None)
            data.setdefault("routines", {})[title] = {"steps": parsed, "created": datetime.now().isoformat(timespec="minutes")}
            self._file.save(data)
        return parsed

    def delete(self, name: str) -> str | None:
        found = self.get(name)
        if not found:
            return None
        with self._file.lock:
            data = self._file.load()
            data.get("routines", {}).pop(found[0], None)
            self._file.save(data)
        return found[0]

    def match(self, text: str) -> str | None:
        """Фраза пользователя — это запуск сценария? («рабочий режим», «запусти сценарий вечер»)."""
        routines = self.all()
        if not routines:
            return None
        key = normalize(text)
        candidates = [key]
        for prefix in _RUN_PREFIXES:
            if key.startswith(prefix + " "):
                candidates.append(key[len(prefix) + 1:].strip())
        for title in routines:
            name = normalize(title)
            if any(candidate == name or (len(name) >= 6 and similarity(candidate, name) >= 0.92)
                   for candidate in candidates):
                return title
        return None

    def prompt_block(self) -> str:
        routines = self.all()
        if not routines:
            return ""
        items = [f"«{name}» ({len(item['steps'])} шаг.)" for name, item in list(routines.items())[:30]]
        return "Сценарии пользователя (запуск — routine с action=run): " + ", ".join(items) + "."


# ═══ Расписание ══════════════════════════════════════════════════════

_DAY_WORDS = {
    "пн": 0, "понедельник": 0, "вт": 1, "вторник": 1, "ср": 2, "среда": 2, "среду": 2, "чт": 3, "четверг": 3,
    "пт": 4, "пятница": 4, "пятницу": 4, "сб": 5, "суббота": 5, "субботу": 5, "вс": 6, "воскресенье": 6,
}
_DAY_GROUPS = {"будни": [0, 1, 2, 3, 4], "по будням": [0, 1, 2, 3, 4], "рабочие дни": [0, 1, 2, 3, 4],
               "выходные": [5, 6], "по выходным": [5, 6], "ежедневно": list(range(7)), "каждый день": list(range(7)),
               "": list(range(7))}
_DAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
STALE_MINUTES = 5  # задача, которую «проспали» дольше (программа была закрыта), не запускается


def parse_days(text: str) -> list[int]:
    key = normalize(text)
    if key in _DAY_GROUPS:
        return list(_DAY_GROUPS[key])
    days = set()
    for word in re.split(r"[\s,;]+", key):
        if not word:
            continue
        if word not in _DAY_WORDS:
            raise RoutineError(f"не понял день «{word}»: используйте «ежедневно», «будни», «выходные» или «пн,ср,пт»")
        days.add(_DAY_WORDS[word])
    return sorted(days)


def parse_time(text: str) -> str:
    match = re.fullmatch(r"\s*(\d{1,2})[:.\s](\d{2})\s*", text or "")
    if not match or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise RoutineError(f"время «{text}» не похоже на ЧЧ:ММ")
    return f"{int(match.group(1)):02d}:{match.group(2)}"


def describe_days(days: list[int]) -> str:
    if sorted(days) == list(range(7)):
        return "ежедневно"
    if sorted(days) == [0, 1, 2, 3, 4]:
        return "по будням"
    if sorted(days) == [5, 6]:
        return "по выходным"
    return ", ".join(_DAY_SHORT[d] for d in sorted(days))


class Schedule:
    def __init__(self, path: Path):
        self._file = JsonFile(path, {"tasks": []})

    def all(self) -> list[dict]:
        return [t for t in self._file.load().get("tasks", []) if isinstance(t, dict)]

    def add(self, name: str, time: str = "", days: str = "", every_minutes: float = 0, routine: str = "",
            prompt: str = "") -> dict:
        name = " ".join(name.split()).strip() or (routine or prompt)[:40]
        if not (routine.strip() or prompt.strip()):
            raise RoutineError("укажите, что делать: routine (название сценария) или prompt (просьба)")
        task = {"id": uuid.uuid4().hex[:8], "name": name, "routine": routine.strip(), "prompt": prompt.strip(),
                "created": datetime.now().isoformat(timespec="seconds"), "last_run": ""}
        if every_minutes:
            if every_minutes < 5:
                raise RoutineError("не чаще одного раза в 5 минут")
            task["every_minutes"] = float(every_minutes)
            task["last_run"] = task["created"]  # первый запуск — через интервал
        else:
            task["time"] = parse_time(time)
            task["days"] = parse_days(days)
        with self._file.lock:
            data = self._file.load()
            data.setdefault("tasks", []).append(task)
            self._file.save(data)
        return task

    def delete(self, name: str) -> list[str]:
        key = normalize(name)
        with self._file.lock:
            data = self._file.load()
            tasks = data.get("tasks", [])
            removed = [t["name"] for t in tasks if t.get("id") == name or normalize(t.get("name", "")) == key
                       or (key and key in normalize(t.get("name", "")))]
            data["tasks"] = [t for t in tasks if t["name"] not in removed]
            if removed:
                self._file.save(data)
        return removed

    @staticmethod
    def describe(task: dict) -> str:
        when = (f"каждые {task['every_minutes']:g} мин" if task.get("every_minutes")
                else f"{describe_days(task.get('days', []))} в {task.get('time')}")
        what = f"сценарий «{task['routine']}»" if task.get("routine") else f"«{task.get('prompt')}»"
        return f"«{task['name']}»: {when} — {what}"

    def due(self, now: datetime | None = None) -> list[dict]:
        """Задачи, которые пора выполнить (и отметка, что они выполнены)."""
        now = now or datetime.now()
        result = []
        with self._file.lock:
            data = self._file.load()
            for task in data.get("tasks", []):
                last = _parse(task.get("last_run"))
                if task.get("every_minutes"):
                    if last is None or now - last >= timedelta(minutes=float(task["every_minutes"])):
                        result.append(task)
                    continue
                if now.weekday() not in task.get("days", []) or not task.get("time"):
                    continue
                hour, minute = map(int, task["time"].split(":"))
                planned = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if planned <= now < planned + timedelta(minutes=STALE_MINUTES) and (last is None or last < planned):
                    result.append(task)
            for task in result:
                task["last_run"] = now.isoformat(timespec="seconds")
            if result:
                self._file.save(data)
        return result

    def today(self, now: datetime | None = None) -> list[tuple[str, dict]]:
        now = now or datetime.now()
        items = [(t["time"], t) for t in self.all() if t.get("time") and now.weekday() in t.get("days", [])
                 and t["time"] >= f"{now:%H:%M}"]
        return sorted(items, key=lambda pair: pair[0])


def _parse(value) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


class Scheduler:
    """Фоновый поток: раз в 15 секунд проверяет расписание и передаёт задачи на выполнение."""

    def __init__(self, schedule: Schedule, on_due: Callable[[dict], None], interval: float = 15.0):
        self.schedule = schedule
        self.on_due = on_due
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                for task in self.schedule.due():
                    self.on_due(task)
            except Exception:  # сбой расписания не должен ронять программу
                import logging

                logging.getLogger(__name__).exception("Ошибка расписания")
