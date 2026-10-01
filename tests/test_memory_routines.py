"""Память, история между запусками, сценарии, расписание, навыки, резервные копии."""

import threading
from datetime import datetime

import pytest

from jarvis.backups import Backups
from jarvis.llm.base import AssistantTurn, ToolCall
from jarvis.memory import HistoryStore, Memory
from jarvis.routines import RoutineError, Routines, Schedule, parse_days, parse_steps, parse_time
from jarvis.skills import Skills, parse_skill_md, slugify
from jarvis.tools import ToolRegistry, ToolServices

from .test_agent import make_agent


def test_memory_remember_update_forget(tmp_path):
    memory = Memory(tmp_path / "memory.json")
    assert memory.remember("Живу в Казани") == "Запомнил: Живу в Казани."
    assert memory.remember("живу в казани.").startswith("Я это уже помню")
    assert memory.remember("Любимый браузер — Firefox").startswith("Запомнил")
    assert memory.remember("Любимый браузер — Chrome").startswith("Обновил")  # уточнение, а не дубль
    assert memory.facts() == ["Живу в Казани", "Любимый браузер — Chrome"]
    block = memory.prompt_block()
    assert "Что ты знаешь о пользователе" in block and "- Живу в Казани" in block
    assert memory.forget("браузер") == ["Любимый браузер — Chrome"]
    assert Memory(tmp_path / "memory.json").facts() == ["Живу в Казани"]  # сохранилось на диск
    memory.remember("Пью кофе без сахара")
    assert len(memory.forget("всё")) == 2 and memory.facts() == [] and memory.prompt_block() == ""


def test_history_survives_restart(tmp_path):
    store = HistoryStore(tmp_path / "history.json")
    call = ToolCall(name="open_app", arguments={"name": "блокнот"}, id="call_1")
    history = [
        {"role": "tool", "tool_call_id": "x", "content": "обрывок"},  # начало без реплики пользователя — отбросится
        {"role": "user", "content": "Открой блокнот"},
        {"role": "assistant", "content": "", "tool_calls": [call], "raw": {"backend": "gemini", "data": object()}},
        {"role": "tool", "tool_call_id": "call_1", "name": "open_app", "content": "Запущено «блокнот»."},
        {"role": "assistant", "content": "Готово, сэр."},
    ]
    store.save(history)
    restored = HistoryStore(tmp_path / "history.json").load()
    assert [m["role"] for m in restored] == ["user", "assistant", "tool", "assistant"]
    assert "raw" not in restored[1]  # «сырые» ответы моделей не сохраняются
    assert restored[1]["tool_calls"][0].id == "call_1" and restored[1]["tool_calls"][0].arguments == {"name": "блокнот"}
    assert restored[2]["tool_call_id"] == "call_1"
    store.clear()
    assert store.load() == []


def test_parse_steps_validation():
    steps = parse_steps('[{"tool": "set_volume", "arguments": {"level": 30}}, {"name": "media", "args": {"action": "next"}}]')
    assert steps == [{"tool": "set_volume", "arguments": {"level": 30}}, {"tool": "media", "arguments": {"action": "next"}}]
    for bad in ("не json", "[]", '[{"tool": "rm_rf"}]', '[{"tool": "routine", "arguments": {}}]', "[1, 2]"):
        with pytest.raises(RoutineError):
            parse_steps(bad)


def test_routines_save_match_delete(tmp_path):
    routines = Routines(tmp_path / "routines.json")
    routines.save("Рабочий режим", '[{"tool": "set_volume", "arguments": {"level": 30}}]')
    assert routines.match("рабочий режим") == "Рабочий режим"
    assert routines.match("Включи рабочий режим") == "Рабочий режим"
    assert routines.match("запусти сценарий рабочий режим") == "Рабочий режим"
    assert routines.match("открой рабочий режим в браузере") is None  # обычная команда — не сценарий
    assert "«Рабочий режим» (1 шаг.)" in routines.prompt_block()
    routines.save("рабочий режим", '[{"tool": "mute", "arguments": {"on": true}}]')  # перезапись по имени
    assert len(routines.all()) == 1 and routines.get("РАБОЧИЙ РЕЖИМ")[1]["steps"][0]["tool"] == "mute"
    assert routines.delete("рабочий режим") == "рабочий режим" and routines.all() == {}


def test_routine_runs_instantly_without_model(config, monkeypatch, tmp_path):
    from jarvis import winapi

    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    agent, backend, events = make_agent(config, [])  # модель не должна понадобиться
    agent.registry.services.routines.save("утро", '[{"tool": "note", "arguments": {"text": "проснулся"}}, '
                                                  '{"tool": "get_datetime", "arguments": {}}]')
    outcome = agent.run("Утро", threading.Event())
    assert backend.calls == [] and outcome.fast and outcome.tools_used == 2
    assert outcome.text == "Сценарий «утро» выполнен, сэр."
    assert [t[0] for t in events.tools] == ["note", "get_datetime"]


def test_routine_tool_save_and_run(config, monkeypatch, tmp_path):
    from jarvis import winapi

    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    steps_seen = []
    services = ToolServices(config)
    services.on_step = lambda name, args, result: steps_seen.append((name, result.status))
    registry = ToolRegistry(services)
    result = registry.execute("routine", {"action": "save", "name": "заметки",
                                          "steps": [{"tool": "note", "arguments": {"text": "раз"}}]})
    assert result.ok and "сохранён" in result.text
    result = registry.execute("routine", {"action": "run", "name": "заметки"})
    assert result.ok and "выполнен" in result.text and steps_seen == [("note", "ok")]
    assert "«заметки»" in registry.execute("routine", {"action": "list"}).text
    assert registry.execute("routine", {"action": "save", "name": "x", "steps": "[]"}).status == "error"


def test_routine_waits_for_launched_window(monkeypatch):
    from jarvis import tools, winapi

    windows, activated = [1], []
    monkeypatch.setattr(winapi, "IS_WINDOWS", True)
    monkeypatch.setattr(winapi, "top_windows", lambda *args, **kwargs: list(windows))
    monkeypatch.setattr(winapi, "foreground_window", lambda: 1)
    monkeypatch.setattr(winapi, "window_title", lambda hwnd: f"окно {hwnd}")
    monkeypatch.setattr(winapi, "window_pid", lambda hwnd: 999)
    monkeypatch.setattr(winapi, "activate_window", lambda hwnd, timeout=1.0: activated.append(hwnd) or True)
    monkeypatch.setattr(tools, "LAUNCH_SETTLE", 0)
    watch = tools.LaunchWatch("open_app", "type_text")
    windows.append(2)  # программа открыла окно, но Windows не вывела его вперёд
    watch.wait(threading.Event(), timeout=2)
    assert activated == [2]  # текст напечатается в новое окно, а не в прежнее
    assert not tools.LaunchWatch("open_app", "set_volume").active  # дальше не ввод — ждать нечего
    assert not tools.LaunchWatch("set_volume", "type_text").active


def test_routine_stops_on_cancel(config, monkeypatch, tmp_path):
    from jarvis import tools, winapi

    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    cancel = threading.Event()
    services = ToolServices(config)
    services.on_step = lambda name, args, result: cancel.set()  # «стоп» после первого шага
    registry = ToolRegistry(services)
    steps = [{"tool": "note", "arguments": {"text": "раз"}}, {"tool": "note", "arguments": {"text": "два"}}]
    results = tools.run_steps(registry, steps, cancel, on_result=services.on_step)
    assert len(results) == 1
    services.routines.save("заметки", steps)
    cancel.clear()
    text = registry.execute("routine", {"action": "run", "name": "заметки"}, cancel).text
    assert "1. note:" in text and "2. остановлено пользователем" in text


def test_schedule_parsing_and_due(tmp_path):
    assert parse_time("9:05") == "09:05" and parse_days("будни") == [0, 1, 2, 3, 4]
    assert parse_days("пн, ср, пт") == [0, 2, 4] and parse_days("") == list(range(7))
    with pytest.raises(RoutineError):
        parse_time("25:00")
    with pytest.raises(RoutineError):
        parse_days("каждый вторник месяца")

    schedule = Schedule(tmp_path / "schedule.json")
    task = schedule.add("брифинг", time="09:00", days="будни", prompt="расскажи брифинг")
    assert schedule.describe(task) == "«брифинг»: по будням в 09:00 — «расскажи брифинг»"
    monday_9 = datetime(2026, 9, 28, 9, 0, 30)  # понедельник
    assert [t["name"] for t in schedule.due(monday_9)] == ["брифинг"]
    assert schedule.due(monday_9.replace(minute=2)) == []  # уже выполнено сегодня
    assert schedule.due(datetime(2026, 9, 29, 9, 30)) == []  # «проспали» больше 5 минут — не запускаем поздно
    assert schedule.due(datetime(2026, 10, 3, 9, 0)) == []  # суббота
    upcoming = schedule.today(datetime(2026, 9, 30, 8, 0))  # среда, 8 утра — брифинг ещё впереди
    assert [(time, task["name"]) for time, task in upcoming] == [("09:00", "брифинг")]
    assert schedule.today(datetime(2026, 9, 30, 10, 0)) == []  # уже прошло
    with pytest.raises(RoutineError):
        schedule.add("пусто", time="10:00")
    assert schedule.delete("брифинг") == ["брифинг"] and schedule.all() == []


def test_schedule_interval(tmp_path):
    schedule = Schedule(tmp_path / "schedule.json")
    task = schedule.add("вода", every_minutes=30, prompt="напомни выпить воды")
    created = datetime.fromisoformat(task["created"])
    assert schedule.due(created.replace(microsecond=0)) == []
    from datetime import timedelta

    assert len(schedule.due(created + timedelta(minutes=31))) == 1
    with pytest.raises(RoutineError):
        schedule.add("часто", every_minutes=1, prompt="x")


def test_schedule_tool_checks_routine_exists(config):
    registry = ToolRegistry(ToolServices(config))
    result = registry.execute("schedule", {"action": "add", "name": "утро", "time": "08:00", "routine": "нет такого"})
    assert result.status == "error" and "сценария" in result.text
    result = registry.execute("schedule", {"action": "add", "name": "кофе", "time": "11:00", "days": "ежедневно",
                                           "prompt": "напомни про кофе"})
    assert result.ok and "ежедневно в 11:00" in result.text
    assert "«кофе»" in registry.execute("schedule", {"action": "list"}).text


def test_skills_agentskills_format(tmp_path):
    skills = Skills(tmp_path / "Skills")
    assert slugify("Сжать картинки!") == "szhat-kartinki"
    skill = skills.save("Сжать картинки", "Уменьшает фото в папке", "1. Найди фото\n2. Сожми", "print('ok')")
    text = (skill.folder / "SKILL.md").read_text(encoding="utf-8")
    assert text.startswith("---\nname: szhat-kartinki\ndescription: Уменьшает фото в папке\n---")
    parsed = parse_skill_md(skill.folder / "SKILL.md")
    assert parsed.name == "szhat-kartinki" and parsed.title == "Сжать картинки" and "Сожми" in parsed.instructions
    assert skills.get("сжать картинки").name == "szhat-kartinki" and skills.get("szhat-kartinki")
    assert skills.approved(skill)
    skill.script.write_text("print('изменён')", encoding="utf-8")
    assert not skills.approved(skill)  # код поменяли после одобрения
    assert "Уменьшает фото" in skills.prompt_block()
    assert skills.delete("сжать картинки") == "Сжать картинки" and skills.all() == []


def test_skill_tool_runs_script_and_asks_for_changed_code(config):
    asked = []
    registry = ToolRegistry(ToolServices(config, confirm=lambda c: asked.append(c) or True))
    code = "import sys\nprint('Привет,', ' '.join(sys.argv[1:]))"
    result = registry.execute("skill", {"action": "save", "name": "Привет", "description": "Здоровается",
                                        "instructions": "Запусти скрипт", "code": code})
    assert result.ok and asked[-1].details == code  # код показан при сохранении
    result = registry.execute("skill", {"action": "use", "name": "привет", "arguments": "сэр Тони"})
    assert "Привет, сэр Тони" in result.text and len(asked) == 1  # одобренный скрипт — без вопросов
    skill = registry.services.skills.get("привет")
    skill.script.write_text("print('другой код')", encoding="utf-8")
    result = registry.execute("skill", {"action": "use", "name": "привет"})
    assert "другой код" in result.text and asked[-1].title == "Запуск навыка"
    assert registry.services.skills.approved(skill)  # после подтверждения снова одобрен


def test_write_file_keeps_backup_and_restore(config, tmp_path, monkeypatch):
    from jarvis import tools

    monkeypatch.setattr(tools.Path, "home", staticmethod(lambda: tmp_path))
    config.set("tools.code_folder", str(tmp_path / "code"))
    registry = ToolRegistry(ToolServices(config, confirm=lambda c: True))
    assert registry.execute("write_file", {"path": "a.py", "content": "v1", "open": False}).ok
    result = registry.execute("write_file", {"path": "a.py", "content": "v2", "open": False})
    assert "restore_file" in result.text
    assert (tmp_path / "code" / "a.py").read_text(encoding="utf-8") == "v2"
    result = registry.execute("restore_file", {"path": "a.py"})
    assert result.ok and "a.py" in result.text
    assert (tmp_path / "code" / "a.py").read_text(encoding="utf-8") == "v1"
    assert registry.execute("restore_file", {}).ok  # откат отката: снова v2
    assert (tmp_path / "code" / "a.py").read_text(encoding="utf-8") == "v2"


def test_backups_without_copies(tmp_path):
    backups = Backups(tmp_path / "Backups")
    with pytest.raises(FileNotFoundError):
        backups.restore("нет.txt")
    assert backups.backup(tmp_path / "нет.txt") is None


def test_memory_and_routines_reach_the_model(config):
    agent, backend, events = make_agent(config, [AssistantTurn(text="Здравствуйте, Иван.")])
    services = agent.registry.services
    services.memory.remember("Меня зовут Иван")
    services.routines.save("кино", '[{"tool": "mute", "arguments": {"on": false}}]')
    agent.context = lambda: "активное окно пользователя: «main.py — Visual Studio Code» (Code.exe)"
    agent.run("Привет", threading.Event())
    system = backend.calls[0]["system"]
    assert "- Меня зовут Иван" in system and "«кино»" in system
    last = backend.calls[0]["messages"][-1]["content"]
    assert last.startswith("Привет") and "main.py — Visual Studio Code" in last
    assert agent.history[0]["content"] == "Привет"  # в историю контекст не попадает


def test_code_tasks_get_more_steps(config, tmp_path, monkeypatch):
    from jarvis import tools

    monkeypatch.setattr(tools.Path, "home", staticmethod(lambda: tmp_path))
    config.set("tools.code_folder", str(tmp_path / "code"))
    write = AssistantTurn(tool_calls=[ToolCall(name="write_file", arguments={"path": "a.py", "content": "x", "open": False})])
    think = AssistantTurn(tool_calls=[ToolCall(name="get_datetime", arguments={})])
    agent, backend, events = make_agent(config, [write] + [think] * 8 + [AssistantTurn(text="Готово.")])
    outcome = agent.run("Напиши программу и проверь, исправь ошибки", threading.Event())
    assert len(backend.calls) == 10 and outcome.text == "Готово."  # больше 5 шагов, но не бесконечно
    assert backend.calls[-1]["allow_tools"] is False and backend.calls[-2]["allow_tools"] is True
