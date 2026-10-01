"""Проактивность: Джарвис сам замечает, что пора вмешаться, и предлагает помощь.

Heartbeat раз в несколько минут делает дешёвые локальные проверки (без обращения к модели —
это бесплатно и не тратит лимиты): диск, батарея, память, процессор. Если что-то не так —
карточка с вопросом «Да / Не сейчас / Не предлагать». Против спама: не больше N предложений
в час, тихие часы, режим «Не беспокоить», пауза после «Не сейчас», а после трёх отказов
подряд такие предложения затихают на неделю. Ещё — утренний брифинг и сводка «пока вас не было».
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import psutil

from .storage import JsonFile

log = logging.getLogger(__name__)

DECLINES_TO_MUTE = 3
AWAY_SECONDS = 15 * 60


@dataclass
class Suggestion:
    kind: str           # тип проверки: disk, battery, memory, cpu…
    text: str           # что сказать пользователю
    prompt: str         # что выполнить, если он согласится
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    created: datetime = field(default_factory=datetime.now)


def _gb(value: float) -> str:
    return f"{value / 1024 ** 3:.1f}".replace(".", ",")


def check_disk() -> Suggestion | None:
    drive = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
    try:
        usage = psutil.disk_usage(drive)
    except OSError:
        return None
    if usage.free < 5 * 1024 ** 3 or usage.percent > 93:
        return Suggestion("disk", f"Сэр, на диске {drive} осталось всего {_gb(usage.free)} ГБ. "
                                  "Показать, что занимает больше всего места?",
                          "Покажи, что занимает больше всего места в моих папках (disk_usage), и посоветуй, что "
                          "можно безопасно удалить. Сам ничего не удаляй.")
    return None


def check_battery() -> Suggestion | None:
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is None or battery.power_plugged or battery.percent >= 20:
        return None
    return Suggestion("battery", f"Сэр, заряд батареи {battery.percent:.0f}%, а зарядка не подключена. "
                                 "Открыть настройки экономии заряда?", "Открой параметры батареи (open_settings).")


def _top_process(key: str) -> psutil.Process | None:
    own = os.getpid()
    best, best_value = None, 0.0
    for proc in psutil.process_iter(["name", "memory_info", "cpu_percent"]):
        if proc.pid in (0, 4, own):
            continue
        info = proc.info
        value = (info["memory_info"].rss if info.get("memory_info") else 0) if key == "memory" else (info.get("cpu_percent") or 0)
        if value > best_value:
            best, best_value = proc, value
    return best


def check_memory() -> Suggestion | None:
    memory = psutil.virtual_memory()
    if memory.percent < 92:
        return None
    top = _top_process("memory")
    if top is None:
        return None
    name = (top.info.get("name") or "?").removesuffix(".exe")
    size = top.info["memory_info"].rss if top.info.get("memory_info") else 0
    return Suggestion(f"memory:{name.lower()}", f"Сэр, память заполнена на {memory.percent:.0f}%. Больше всего "
                                                f"занимает {name} ({_gb(size)} ГБ). Закрыть его?", f"Закрой {name}")


class CpuWatch:
    """Процесс, который грузит процессор две проверки подряд."""

    def __init__(self):
        self._previous: str = ""
        for proc in psutil.process_iter():  # первый вызов cpu_percent всегда 0 — «заводим» счётчики
            try:
                proc.cpu_percent(None)
            except psutil.Error:
                pass

    def check(self) -> Suggestion | None:
        if psutil.cpu_percent(interval=None) < 85:
            self._previous = ""
            return None
        top = _top_process("cpu")
        if top is None or (top.info.get("cpu_percent") or 0) < 50 * max(1, psutil.cpu_count() or 1) / 4:
            self._previous = ""
            return None
        name = (top.info.get("name") or "?").removesuffix(".exe")
        if self._previous != name:
            self._previous = name
            return None
        return Suggestion(f"cpu:{name.lower()}", f"Сэр, {name} уже несколько минут сильно грузит процессор. "
                                                 "Закрыть его?", f"Закрой {name}")


_YES_CORE = {"да", "давай", "конечно", "хорошо", "ага", "угу", "окей", "ок", "согласен", "можно", "валяй",
             "покажи", "сделай"}
_NO_CORE = {"нет", "не", "потом", "позже", "отстань"}
_FILLER = {"сэр", "ну", "пожалуйста", "спасибо"}
_YES_WORDS = _YES_CORE | _FILLER
_NO_WORDS = _NO_CORE | _FILLER | {"надо", "нужно", "сейчас", "пока"}


def suggestion_answer(words: str) -> str | None:
    """Короткий ответ голосом на предложение: «да, давай» → yes, «нет, не сейчас» → later.
    Всё остальное («сделай погромче») — новая просьба, а не ответ."""
    tokens = set(words.split())
    if not tokens or len(words.split()) > 4:
        return None
    if tokens <= _NO_WORDS and tokens & _NO_CORE:
        return "later"
    if tokens <= _YES_WORDS and tokens & _YES_CORE:
        return "yes"
    return None


def _in_quiet_hours(now: datetime, spec: str) -> bool:
    """«23:00-08:00» → тихо с 23 до 8 утра."""
    try:
        start, end = [datetime.strptime(part.strip(), "%H:%M").time() for part in spec.split("-")]
    except (ValueError, AttributeError):
        return False
    current = now.time()
    return start <= current < end if start <= end else (current >= start or current < end)


class Proactive:
    def __init__(self, config, state_path: Path, deliver: Callable[[Suggestion], None],
                 on_briefing: Callable[[], None], on_return: Callable[[str], None],
                 idle_seconds: Callable[[], float] = lambda: 0.0, busy: Callable[[], bool] = lambda: False):
        self.config = config
        self.deliver = deliver
        self.on_briefing = on_briefing
        self.on_return = on_return
        self.idle_seconds = idle_seconds
        self.busy = busy
        self._state = JsonFile(state_path, {})
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cpu: CpuWatch | None = None
        self._last_check = 0.0
        self._away_since: datetime | None = None
        self._away_events: list[str] = []
        self._queued: list[Suggestion] = []
        self.checks: list[Callable[[], Suggestion | None]] = [check_disk, check_battery, check_memory]

    # ─── настройки ───
    @property
    def enabled(self) -> bool:
        return bool(self.config.get("proactive.enabled", True))

    @property
    def dnd(self) -> bool:
        return bool(self.config.get("proactive.dnd", False))

    def quiet(self, now: datetime) -> bool:
        return self.dnd or _in_quiet_hours(now, str(self.config.get("proactive.quiet_hours", "23:00-08:00")))

    # ─── поток ───
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="proactive", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        self._cpu = CpuWatch()
        self.checks.append(self._cpu.check)
        while not self._stop.wait(30):
            try:
                self.tick(datetime.now())
            except Exception:
                log.exception("Ошибка проактивных проверок")

    def tick(self, now: datetime) -> None:
        if not self.enabled:
            return
        idle = self.idle_seconds()
        self._track_presence(now, idle)
        if self._away_since is None and idle < 120:
            self._maybe_briefing(now)
        interval = max(1.0, float(self.config.get("proactive.interval_minutes", 10))) * 60
        if self._last_check and (now.timestamp() - self._last_check) < interval:
            return
        self._last_check = now.timestamp()
        for check in list(self.checks):
            try:
                suggestion = check()
            except Exception as exc:
                log.debug("Проверка %s не удалась: %s", getattr(check, "__name__", check), exc)
                continue
            if suggestion is not None:
                self.offer(suggestion, now)

    # ─── предложения ───
    def allowed(self, kind: str, now: datetime) -> bool:
        if self.quiet(now) or self.busy():
            return False
        state = self._state.load()
        muted = state.get("muted", {}).get(kind)
        if muted == "forever" or (muted and datetime.fromisoformat(muted) > now):
            return False
        last = state.get("last", {}).get(kind)
        cooldown = timedelta(hours=float(self.config.get("proactive.cooldown_hours", 6)))
        if last and now - datetime.fromisoformat(last) < cooldown:
            return False
        recent = [t for t in state.get("shown", []) if now - datetime.fromisoformat(t) < timedelta(hours=1)]
        return len(recent) < int(self.config.get("proactive.max_per_hour", 2))

    def offer(self, suggestion: Suggestion, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        if not self.allowed(suggestion.kind, now):
            return False
        with self._state.lock:
            state = self._state.load()
            state.setdefault("last", {})[suggestion.kind] = now.isoformat(timespec="seconds")
            shown = [t for t in state.get("shown", []) if now - datetime.fromisoformat(t) < timedelta(hours=1)]
            state["shown"] = shown + [now.isoformat(timespec="seconds")]
            self._state.save(state)
        if self._away_since is not None:  # пользователя нет — покажем, когда вернётся
            self._queued.append(suggestion)
            return True
        self.deliver(suggestion)
        return True

    def feedback(self, kind: str, answer: str, now: datetime | None = None) -> None:
        """answer: yes — согласился, later — «не сейчас», never — «не предлагать»."""
        now = now or datetime.now()
        with self._state.lock:
            state = self._state.load()
            declines = state.setdefault("declines", {})
            muted = state.setdefault("muted", {})
            if answer == "yes":
                declines[kind] = 0
            elif answer == "never":
                muted[kind] = "forever"
            else:
                declines[kind] = declines.get(kind, 0) + 1
                pause = timedelta(days=7) if declines[kind] >= DECLINES_TO_MUTE else timedelta(hours=24)
                muted[kind] = (now + pause).isoformat(timespec="seconds")
            self._state.save(state)

    def muted_kinds(self) -> list[str]:
        return [kind for kind, until in self._state.load().get("muted", {}).items() if until]

    def unmute_all(self) -> None:
        with self._state.lock:
            state = self._state.load()
            state["muted"], state["declines"] = {}, {}
            self._state.save(state)

    # ─── утро и «пока вас не было» ───
    def _maybe_briefing(self, now: datetime) -> None:
        if not self.config.get("proactive.morning_briefing", True) or self.dnd or not (5 <= now.hour < 12):
            return
        with self._state.lock:
            state = self._state.load()
            if state.get("briefing_date") == now.date().isoformat():
                return
            state["briefing_date"] = now.date().isoformat()
            self._state.save(state)
        self.on_briefing()

    def note_event(self, text: str) -> None:
        """Что случилось, пока пользователя не было (сработал таймер, выполнено расписание…)."""
        if self._away_since is not None:
            self._away_events.append(f"{datetime.now():%H:%M} — {text}")

    def _track_presence(self, now: datetime, idle: float) -> None:
        if self._away_since is None:
            if idle >= AWAY_SECONDS:
                self._away_since = now - timedelta(seconds=idle)
            return
        if idle > 60:
            return
        away_for = now - self._away_since
        events, queued = self._away_events, self._queued
        self._away_since, self._away_events, self._queued = None, [], []
        if events:
            minutes = int(away_for.total_seconds() // 60)
            duration = f"{minutes // 60} ч {minutes % 60} мин" if minutes >= 60 else f"{minutes} мин"
            self.on_return(f"Пока вас не было ({duration}):\n" + "\n".join(f"• {e}" for e in events[-10:]))
        for suggestion in queued[-2:]:
            self.deliver(suggestion)
