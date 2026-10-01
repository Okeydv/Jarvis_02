"""Проактивность (heartbeat, лимиты, отказы, «пока вас не было»), брифинг и зрение."""

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from jarvis import briefing, proactive, tools, vision
from jarvis.llm.base import LLMBackend, LLMError
from jarvis.proactive import Proactive, Suggestion
from jarvis.tools import ToolRegistry, ToolServices

NOON = datetime(2026, 10, 1, 12, 0)


class Recorder:
    def __init__(self):
        self.suggestions, self.briefings, self.returns = [], [], []


@pytest.fixture
def heartbeat(config, tmp_path):
    record = Recorder()
    idle = {"seconds": 0.0}
    engine = Proactive(config, tmp_path / "proactive.json", deliver=record.suggestions.append,
                       on_briefing=lambda: record.briefings.append(1), on_return=record.returns.append,
                       idle_seconds=lambda: idle["seconds"])
    engine.checks = []
    engine.record, engine.idle = record, idle
    return engine


def test_rate_limits_quiet_hours_and_dnd(config, heartbeat):
    assert heartbeat.offer(Suggestion("disk", "Мало места", "покажи"), NOON)
    assert not heartbeat.offer(Suggestion("disk", "Мало места", "покажи"), NOON + timedelta(minutes=5))  # пауза
    assert heartbeat.offer(Suggestion("battery", "Батарея", "открой"), NOON + timedelta(minutes=6))
    assert not heartbeat.offer(Suggestion("memory:chrome", "Память", "закрой"), NOON + timedelta(minutes=7))  # 2 в час
    assert heartbeat.offer(Suggestion("memory:chrome", "Память", "закрой"), NOON + timedelta(minutes=70))
    assert not heartbeat.offer(Suggestion("cpu:x", "ЦП", "закрой"), datetime(2026, 10, 1, 23, 30))  # тихие часы
    config.set("proactive.dnd", True)
    assert not heartbeat.offer(Suggestion("cpu:y", "ЦП", "закрой"), NOON + timedelta(hours=3))
    assert [s.kind for s in heartbeat.record.suggestions] == ["disk", "battery", "memory:chrome"]


def test_learns_from_answers(heartbeat):
    later = NOON
    for _ in range(3):  # три «Не сейчас» подряд — тема замолкает на неделю
        later += timedelta(days=2)
        heartbeat.feedback("disk", "later", later)
    assert not heartbeat.allowed("disk", later + timedelta(days=6))
    assert heartbeat.allowed("disk", later + timedelta(days=8))
    heartbeat.feedback("battery", "never", NOON)
    assert not heartbeat.allowed("battery", NOON + timedelta(days=365))
    assert "battery" in heartbeat.muted_kinds()
    heartbeat.unmute_all()
    assert heartbeat.allowed("battery", NOON + timedelta(days=1))


def test_checks_run_on_interval(config, heartbeat):
    calls = []
    heartbeat.checks = [lambda: calls.append(1) or Suggestion("disk", "Мало места", "покажи")]
    heartbeat.tick(NOON)
    heartbeat.tick(NOON + timedelta(minutes=3))  # интервал — 10 минут
    assert len(calls) == 1 and len(heartbeat.record.suggestions) == 1
    heartbeat.tick(NOON + timedelta(minutes=11))
    assert len(calls) == 2
    config.set("proactive.enabled", False)
    heartbeat.tick(NOON + timedelta(minutes=30))
    assert len(calls) == 2


def test_morning_briefing_once_a_day(heartbeat):
    heartbeat.tick(datetime(2026, 10, 1, 8, 30))
    heartbeat.tick(datetime(2026, 10, 1, 9, 30))
    assert heartbeat.record.briefings == [1]
    heartbeat.tick(datetime(2026, 10, 2, 13, 0))  # после полудня — уже не утро
    heartbeat.tick(datetime(2026, 10, 3, 7, 0))
    assert heartbeat.record.briefings == [1, 1]


def test_while_you_were_away(heartbeat):
    heartbeat.idle["seconds"] = 20 * 60  # ушёл
    heartbeat.tick(NOON)
    heartbeat.note_event("по расписанию «бэкап»")
    heartbeat.offer(Suggestion("disk", "Мало места", "покажи"), NOON + timedelta(minutes=1))
    assert heartbeat.record.suggestions == []  # без пользователя не показываем
    heartbeat.idle["seconds"] = 5  # вернулся
    heartbeat.tick(NOON + timedelta(minutes=40))
    assert heartbeat.record.returns and "Пока вас не было" in heartbeat.record.returns[0]
    assert "по расписанию «бэкап»" in heartbeat.record.returns[0]
    assert [s.kind for s in heartbeat.record.suggestions] == ["disk"]  # отложенное предложение — сейчас


def test_voice_answers_to_suggestion():
    answer = proactive.suggestion_answer
    assert answer("да") == answer("да давай") == answer("конечно сэр") == answer("давай") == "yes"
    assert answer("нет") == answer("не сейчас") == answer("нет спасибо") == answer("не надо") == "later"
    assert answer("нот да давай") == "yes"  # «…блокнот?» из колонок + ответ
    assert answer("сделай погромче") is None and answer("да открой браузер и включи музыку") is None
    assert answer("") is None and answer("какая погода") is None


def test_disk_and_battery_checks(monkeypatch):
    monkeypatch.setattr(proactive.psutil, "disk_usage", lambda path: SimpleNamespace(free=3 * 1024 ** 3, percent=97))
    suggestion = proactive.check_disk()
    assert suggestion.kind == "disk" and "3,0 ГБ" in suggestion.text and "disk_usage" in suggestion.prompt
    monkeypatch.setattr(proactive.psutil, "disk_usage", lambda path: SimpleNamespace(free=300 * 1024 ** 3, percent=40))
    assert proactive.check_disk() is None
    monkeypatch.setattr(proactive.psutil, "sensors_battery",
                        lambda: SimpleNamespace(percent=12, power_plugged=False), raising=False)
    assert "12%" in proactive.check_battery().text
    monkeypatch.setattr(proactive.psutil, "sensors_battery",
                        lambda: SimpleNamespace(percent=12, power_plugged=True), raising=False)
    assert proactive.check_battery() is None


def test_briefing(config, monkeypatch):
    services = ToolServices(config)
    services.memory.remember("Я живу в Казани")
    services.reminders.append({"when": datetime(2026, 10, 1, 18, 30), "label": "позвонить маме"})
    services.schedule.add("спорт", time="19:00", days="ежедневно", prompt="напомни про спорт")
    cities = []

    def fake_weather(ctx, city=""):
        cities.append(city)
        if city != "Казань":  # «Казани» сервис погоды не знает — пробуется именительный падеж
            raise tools.ToolError("Unknown location")
        return "Погода: Казань. +8°C."

    monkeypatch.setattr(tools, "weather", fake_weather)
    text = briefing.compose(services, now=datetime(2026, 10, 1, 8, 15))
    assert text.startswith("Доброе утро, сэр. Сегодня четверг, 1 октября, 08:15.")
    assert "Погода: Казань. +8°C." in text and cities == ["Казань"]  # сначала именительный падеж
    assert briefing.city_variants("Москве") == ["Москва", "Москв", "Москве"]
    assert briefing.city_variants("Новосибирске")[0] == "Новосибирск"
    assert briefing.city_variants("Петербурге")[0] == "Петербург"
    assert briefing.city_variants("Сочи") == ["Сочь", "Сочи"]
    services.memory.forget("всё")
    services.memory.remember("Город — Нижний Новгород")
    assert briefing.home_city(services) == "Нижний Новгород"
    assert "в 18:30 — позвонить маме" in text and "в 19:00 — спорт" in text


class SeeingBackend(LLMBackend):
    name = "gemini"
    title = "Gemini"

    def __init__(self, config, answer):
        super().__init__(config)
        self.answer = answer
        self.prompts = []

    @property
    def model(self):
        return "fake"

    def stream_chat(self, *args, **kwargs):
        raise NotImplementedError

    def supports_vision(self):
        return True

    def vision(self, prompt, image, mime="image/jpeg"):
        self.prompts.append(prompt)
        assert image[:2] == b"\xff\xd8"  # JPEG
        return self.answer


class BlindBackend(SeeingBackend):
    name = "ollama"

    def supports_vision(self):
        return False


def screenshot():
    from PIL import Image

    return Image.new("RGB", (2000, 1000), (20, 30, 40))


def test_locate_scales_to_screen(config):
    backend = SeeingBackend(config, 'Вот: {"found": true, "x": 500, "y": 250, "label": "кнопка «Отправить»"}')
    service = vision.VisionService(config, lambda name: backend, lambda: "gemini")
    location = service.locate("кнопка отправить", image=screenshot())
    assert (location.x, location.y) == (1000, 250) and location.label == "кнопка «Отправить»"
    assert location.preview.size == (360, 360)
    backend.answer = '{"found": false}'
    assert service.locate("ничего", image=screenshot()) is None
    assert vision.parse_location("не JSON") is None and vision.parse_location('{"x": 5000, "y": 1}') is None


def test_vision_backend_fallback(config, monkeypatch):
    seeing = SeeingBackend(config, "На экране Блокнот.")
    created = []

    def get_backend(name):
        created.append(name)
        return BlindBackend(config, "") if name == "ollama" else seeing

    monkeypatch.setenv("BAZAARLINK_API_KEY", "sk-bl-x")
    service = vision.VisionService(config, get_backend, lambda: "ollama")
    assert service.look("Что на экране?", image=screenshot()) == "На экране Блокнот."
    assert created == ["ollama", "qwen"]  # локальная модель не видит — взяли Qwen с ключом

    monkeypatch.delenv("BAZAARLINK_API_KEY")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    blind = vision.VisionService(config, lambda name: BlindBackend(config, ""), lambda: "ollama")
    with pytest.raises(LLMError):
        blind.look("?", image=screenshot())


def test_screen_tool_click_with_preview_confirmation(config, monkeypatch):
    from jarvis import tools

    pressed = []
    monkeypatch.setattr(tools, "_pyautogui", lambda: SimpleNamespace(
        click=lambda x, y, clicks=1, interval=0.0: pressed.append((x, y, clicks))))
    monkeypatch.setattr(vision, "capture", screenshot)
    backend = SeeingBackend(config, '{"found": true, "x": 100, "y": 900, "label": "Пуск"}')
    asked = []
    services = ToolServices(config, confirm=lambda c: asked.append(c) or True)
    services.vision = vision.VisionService(config, lambda name: backend, lambda: "gemini")
    registry = ToolRegistry(services)
    result = registry.execute("screen", {"action": "click", "target": "кнопка Пуск"})
    assert result.ok and result.text == "Нажал на «Пуск»."
    assert pressed == [(200, 899, 1)] and asked[0].image is not None  # подтверждение со снимком места клика
    config.set("tools.confirm_clicks", False)
    assert registry.execute("screen", {"action": "click", "target": "Пуск", "double": True}).ok
    assert pressed[-1][2] == 2 and len(asked) == 1
    backend.answer = "На экране рабочий стол."
    assert registry.execute("screen", {"action": "look"}).text == "На экране: На экране рабочий стол."
    backend.answer = '{"found": false}'
    assert registry.execute("screen", {"action": "click", "target": "x"}).status == "error"
