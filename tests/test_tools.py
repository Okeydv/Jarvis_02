import json
import re
import threading

import pytest

from jarvis import tools, winapi
from jarvis.tools import (
    TOOLS,
    Confirmation,
    ToolError,
    ToolRegistry,
    ToolServices,
    parse_hotkey,
    resolve_folder,
    validate_arguments,
)

DANGEROUS = {"close_app", "sleep_pc", "shutdown_pc", "restart_pc", "run_powershell"}
CYRILLIC = re.compile("[а-яё]", re.IGNORECASE)


class Recorder:
    def __init__(self, answer):
        self.answer = answer
        self.asked: list[Confirmation] = []

    def __call__(self, confirmation):
        self.asked.append(confirmation)
        return self.answer


def make_registry(config, answer=False, **overrides):
    recorder = Recorder(answer)
    services = ToolServices(config, confirm=recorder, **overrides)
    return ToolRegistry(services), recorder


def test_required_tools_present():
    required = {"open_app", "close_app", "open_url", "web_search", "open_folder", "set_volume", "mute",
                "media", "type_text", "hotkey", "screenshot", "system_info", "get_datetime", "timer",
                "note", "lock_pc", "sleep_pc", "shutdown_pc", "restart_pc", "run_powershell"}
    assert required <= set(TOOLS)


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_schema_is_valid_and_russian(name):
    item = TOOLS[name]
    schema = item.schema()
    json.dumps(schema, ensure_ascii=False)
    assert CYRILLIC.search(item.description)
    params = schema["parameters"]
    assert params["type"] == "object"
    assert set(params["required"]) <= set(params["properties"])
    for spec in params["properties"].values():
        assert spec["type"] in {"string", "integer", "number", "boolean"}
        assert CYRILLIC.search(spec["description"])


def test_powershell_disabled_by_default(config):
    registry, _ = make_registry(config)
    assert "run_powershell" not in [s["name"] for s in registry.schemas()]
    result = registry.execute("run_powershell", {"command": "dir"})
    assert result.status == "error" and "отключён" in result.text
    config.set("tools.allow_powershell", True)
    assert "run_powershell" in [s["name"] for s in registry.schemas()]


def test_unknown_tool_is_rejected(config):
    registry, _ = make_registry(config)
    result = registry.execute("format_disk", {"drive": "C"})
    assert result.status == "error"
    assert "не существует" in result.text


def test_validation_and_coercion():
    schema = TOOLS["set_volume"].parameters
    assert validate_arguments(schema, {"level": "45"}) == {"level": 45}
    assert validate_arguments(schema, '{"level": 30.4}') == {"level": 30}
    with pytest.raises(ToolError):
        validate_arguments(schema, {"level": 101})
    with pytest.raises(ToolError):
        validate_arguments(schema, {})
    assert validate_arguments(TOOLS["mute"].parameters, {"on": "да"}) == {"on": True}
    assert validate_arguments(TOOLS["media"].parameters, {"action": "NEXT"}) == {"action": "next"}
    with pytest.raises(ToolError):
        validate_arguments(TOOLS["media"].parameters, {"action": "stop"})
    assert validate_arguments(TOOLS["hotkey"].parameters, {"keys": ["ctrl", "c"]}) == {"keys": "ctrl+c"}
    # неизвестные параметры игнорируются
    assert validate_arguments(TOOLS["open_app"].parameters, {"name": "блокнот", "evil": 1}) == {"name": "блокнот"}


def test_tool_exception_becomes_error_text(config, monkeypatch):
    registry, _ = make_registry(config)
    monkeypatch.setattr(TOOLS["get_datetime"], "func", lambda ctx: 1 / 0)
    result = registry.execute("get_datetime", {})
    assert result.status == "error" and "ZeroDivisionError" in result.text


def _fake_processes(monkeypatch):
    class FakeProc:
        pid = 4242
        info = {"name": "notepad.exe", "pid": 4242}

    monkeypatch.setattr(tools, "find_app_processes", lambda services, name: ("блокнот", [FakeProc()], False))
    return FakeProc


@pytest.mark.parametrize("name, args", [
    ("close_app", {"name": "блокнот"}),
    ("sleep_pc", {}),
    ("shutdown_pc", {}),
    ("restart_pc", {}),
    ("run_powershell", {"command": "Remove-Item C:\\temp -Recurse"}),
])
def test_dangerous_tools_need_confirmation(config, monkeypatch, name, args):
    config.set("tools.allow_powershell", True)
    _fake_processes(monkeypatch)
    called = []
    monkeypatch.setattr(TOOLS[name], "func", lambda ctx, **kw: called.append(kw) or "done")

    registry, recorder = make_registry(config, answer=False)
    result = registry.execute(name, args)
    assert result.status == "cancelled"
    assert called == []
    assert len(recorder.asked) == 1 and recorder.asked[0].text

    registry, recorder = make_registry(config, answer=True)
    result = registry.execute(name, args)
    assert result.ok and called == [args]


def test_no_confirm_handler_means_denied(config, monkeypatch):
    monkeypatch.setattr(TOOLS["shutdown_pc"], "func", lambda ctx: pytest.fail("выключение без подтверждения"))
    registry = ToolRegistry(ToolServices(config))
    assert registry.execute("shutdown_pc", {}).status == "cancelled"


def test_powershell_confirmation_shows_full_command(config, monkeypatch):
    config.set("tools.allow_powershell", True)
    monkeypatch.setattr(TOOLS["run_powershell"], "func", lambda ctx, command: "ok")
    registry, recorder = make_registry(config, answer=False)
    command = "Get-ChildItem C:\\ | Where-Object { $_.Length -gt 1GB } | Remove-Item -WhatIf"
    registry.execute("run_powershell", {"command": command})
    assert recorder.asked[0].details == command


def test_hotkey_alt_f4_needs_confirmation(config, monkeypatch):
    sent = []

    class FakeGui:
        @staticmethod
        def hotkey(*keys, interval=0.0):
            sent.append(keys)

    class FakeFocus:
        def target(self):
            return 777

        def focus_target(self):
            return 777

    monkeypatch.setattr(tools, "_pyautogui", lambda: FakeGui)
    monkeypatch.setattr(winapi, "window_title", lambda hwnd: "Документ — Блокнот")
    monkeypatch.setattr(winapi, "is_window", lambda hwnd: True)
    monkeypatch.setattr(winapi, "activate_window", lambda hwnd, timeout=1.0: True)
    monkeypatch.setattr(winapi, "foreground_window", lambda: 777)

    registry, recorder = make_registry(config, answer=False, focus=FakeFocus())
    assert registry.execute("hotkey", {"keys": "alt+f4"}).status == "cancelled"
    assert "Блокнот" in recorder.asked[0].text
    assert sent == []

    assert registry.execute("hotkey", {"keys": "ctrl+c"}).ok
    assert sent == [("ctrl", "c")]
    assert len(recorder.asked) == 1  # для ctrl+c подтверждение не спрашивали

    registry, recorder = make_registry(config, answer=True, focus=FakeFocus())
    assert registry.execute("hotkey", {"keys": "Alt+F4"}).ok
    assert sent[-1] == ("alt", "f4")


def test_parse_hotkey():
    assert parse_hotkey("Ctrl+Shift+Esc") == ["ctrl", "shift", "esc"]
    assert parse_hotkey("shift + ctrl + t") == ["ctrl", "shift", "t"]
    assert parse_hotkey("контрол+с") == ["ctrl", "c"]  # русская раскладка → физическая клавиша
    assert parse_hotkey("win d") == ["win", "d"]
    with pytest.raises(ToolError):
        parse_hotkey("ctrl+непонятно")


def test_app_catalog(config):
    catalog = ToolServices(config).apps
    assert catalog.find("Блокнот").run == ["notepad.exe"]
    assert catalog.find("телеграмм").process == ["Telegram.exe"]
    assert catalog.find("Telegram").title == "телеграм"
    assert catalog.find("программу калькулятор").title == "калькулятор"
    assert catalog.find("Google Chrome").process == ["chrome.exe"]
    assert catalog.find("совсем неизвестное приложение") is None


def test_open_url_validation(config, monkeypatch):
    opened = []
    monkeypatch.setattr(tools.webbrowser, "open", lambda url, new=0: opened.append(url))
    registry, _ = make_registry(config)
    assert registry.execute("open_url", {"url": "youtube.com"}).ok
    assert opened == ["https://youtube.com"]
    for bad in ("javascript:alert(1)", "file:///C:/Windows", "not a url"):
        assert registry.execute("open_url", {"url": bad}).status == "error"
    assert registry.execute("web_search", {"query": "погода в Москве"}).ok
    assert "q=%D0%BF%D0%BE%D0%B3%D0%BE%D0%B4%D0%B0" in opened[-1]


def test_resolve_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    assert resolve_folder("Загрузки") == tmp_path / "Downloads"
    assert resolve_folder("рабочий стол") == tmp_path / "Desktop"
    assert resolve_folder("Документы\\Jarvis") == tmp_path / "Documents" / "Jarvis"
    assert resolve_folder("Корзина") == "shell:RecycleBinFolder"


def test_note_and_timer(config, monkeypatch, tmp_path):
    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    notified = threading.Event()
    registry, _ = make_registry(config, notify=lambda title, text: notified.set())
    result = registry.execute("note", {"text": "купить молоко"})
    assert result.ok
    content = (tmp_path / "Documents" / "Jarvis" / "notes.txt").read_text(encoding="utf-8")
    assert "купить молоко" in content

    result = tools.timer(tools.CallContext(registry.services, None), minutes=0.002, label="чай")
    assert "чай" in result
    assert notified.wait(3)


def test_get_datetime_is_russian(config):
    registry, _ = make_registry(config)
    text = registry.execute("get_datetime", {}).text
    assert re.search(r"Сейчас \d\d:\d\d, (понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)", text)


def test_close_app_refuses_protected_and_own_processes(config):
    services = ToolServices(config)
    title, processes, explorer = tools.find_app_processes(services, "python")
    import os

    assert all(p.pid != os.getpid() for p in processes)
    assert not explorer
    title, processes, explorer = tools.find_app_processes(services, "проводник")
    assert explorer and processes == []
