"""Брифинг: «Доброе утро, сэр. Сегодня…» — дата, погода, напоминания и расписание на сегодня,
состояние компьютера."""

from __future__ import annotations

import os
import re
from datetime import datetime

import psutil

_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
_MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря"]


def greeting(now: datetime) -> str:
    if 5 <= now.hour < 12:
        return "Доброе утро"
    if 12 <= now.hour < 17:
        return "Добрый день"
    if 17 <= now.hour < 23:
        return "Добрый вечер"
    return "Доброй ночи"


def home_city(services) -> str:
    """Город для погоды: из настроек или из памяти («живу в Казани», «мой город — Казань»)."""
    city = str(services.config.get("briefing.city", "") or "").strip()
    if city:
        return city
    for fact in services.memory.facts():
        match = re.search(r"(?:живу|нахожусь|живём|живем)\s+(?:в|во)\s+([А-ЯЁA-Z][\w-]+(?:[\s-][А-ЯЁA-Z][\w-]+)?)", fact)
        if match:
            return match.group(1)
        match = re.search(r"(?:^|(?:мой|наш)\s+)город\s*[—:-]?\s*([А-ЯЁA-Z][\w-]+(?:[\s-][А-ЯЁA-Z][\w-]+)?)",
                          fact, re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def city_variants(city: str) -> list[str]:
    """Город из фразы «живу в …» стоит в предложном падеже, а сервис погоды ищет по именительному:
    Казани → Казань, Москве → Москва, Новосибирске → Новосибирск. Исходное слово — последним."""
    variants = []
    if city.endswith("и"):
        variants += [city[:-1] + "ь"]
    elif re.search(r"(?:ск|бург|град|ер|ов|ин|ев|ий|ом|ам|ан)е$", city):
        variants += [city[:-1]]
    elif city.endswith("е"):
        variants += [city[:-1] + "а", city[:-1]]
    variants.append(city)
    return [v for i, v in enumerate(variants) if v not in variants[:i]]


def system_state() -> str:
    problems = []
    drive = os.environ.get("SystemDrive", "C:") + "\\" if os.name == "nt" else "/"
    try:
        usage = psutil.disk_usage(drive)
        if usage.free < 5 * 1024 ** 3 or usage.percent > 92:
            problems.append(f"на диске {drive} осталось {usage.free / 1024 ** 3:.1f} ГБ".replace(".", ","))
    except OSError:
        pass
    battery = psutil.sensors_battery() if hasattr(psutil, "sensors_battery") else None
    if battery is not None and not battery.power_plugged and battery.percent < 30:
        problems.append(f"батарея {battery.percent:.0f}%")
    if psutil.virtual_memory().percent > 90:
        problems.append("память почти заполнена")
    return ("Компьютер: " + ", ".join(problems) + ".") if problems else "С компьютером всё в порядке."


def compose(services, now: datetime | None = None, weather: bool = True) -> str:
    from . import tools
    from .tools import CallContext

    weather_tool = tools.weather

    now = now or datetime.now()
    parts = [f"{greeting(now)}, сэр. Сегодня {_WEEKDAYS[now.weekday()]}, {now.day} {_MONTHS[now.month - 1]}, "
             f"{now:%H:%M}."]
    if weather:
        for city in city_variants(home_city(services)):
            try:
                parts.append(weather_tool(CallContext(services, None), city=city))
                break
            except Exception:  # без сети или с неизвестным городом брифинг всё равно нужен
                continue
    upcoming = sorted((r for r in services.reminders if r["when"] > now and r["when"].date() == now.date()),
                      key=lambda r: r["when"])
    plans = [f"в {r['when']:%H:%M} — {r['label']}" for r in upcoming]
    plans += [f"в {time} — {task['name']}" for time, task in services.schedule.today(now)]
    parts.append(("На сегодня: " + "; ".join(plans) + ".") if plans else "Напоминаний на сегодня нет.")
    parts.append(system_state())
    return " ".join(parts)
