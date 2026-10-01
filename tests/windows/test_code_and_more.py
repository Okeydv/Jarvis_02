"""Код, буфер обмена, окна, поиск файлов и погода на настоящей Windows."""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("проверки только для Windows", allow_module_level=True)

import os  # noqa: E402
import time  # noqa: E402

from jarvis import winapi  # noqa: E402
from jarvis.tools import code_folder, find_editor  # noqa: E402

from .conftest import kill_all, processes, wait_for  # noqa: E402

EDITORS = ("code.exe", "cursor.exe", "notepad++.exe", "notepad.exe")


def test_write_code_open_in_editor_and_run(config, registry, confirmer):
    for name in EDITORS:
        kill_all(name)
    code = "import sys\nprint('Привет, сэр!', sum(range(101)))\nprint(sys.version_info[:2])\n"
    result = registry.execute("write_file", {"path": "jarvis_test_sum.py", "content": code})
    try:
        assert result.ok and "Открыт в редакторе" in result.text, result.text
        target = code_folder(config) / "jarvis_test_sum.py"
        assert target.read_text(encoding="utf-8") == code
        editor = find_editor(config)
        print("Редактор:", editor)
        assert wait_for(lambda: any(processes(name) for name in EDITORS), 20), "редактор не запустился"

        confirmer.answer = True
        result = registry.execute("run_python", {"path": "jarvis_test_sum.py"})
        assert result.ok and "Привет, сэр! 5050" in result.text, result.text
        assert confirmer.asked[-1].details == code  # перед запуском показан весь код

        result = registry.execute("run_python", {"code": "print(1/0)"})
        assert "ZeroDivisionError" in result.text and "Код завершения 1" in result.text
    finally:
        for name in EDITORS:
            kill_all(name)
        (code_folder(config) / "jarvis_test_sum.py").unlink(missing_ok=True)
        (code_folder(config) / "jarvis_run.py").unlink(missing_ok=True)


def test_clipboard_round_trip(registry):
    text = "Буфер обмена: привет, сэр! 🙂 Line 2\nСтрока 3"
    assert registry.execute("clipboard", {"action": "set", "text": text}).ok
    assert winapi.get_clipboard_text().replace("\r\n", "\n") == text
    result = registry.execute("clipboard", {"action": "get"})
    assert result.ok and "привет, сэр! 🙂" in result.text


def test_window_actions_with_notepad(registry):
    kill_all("notepad.exe")
    try:
        assert registry.execute("open_app", {"name": "блокнот"}).ok
        assert wait_for(lambda: processes("notepad.exe"), 15)
        time.sleep(1.5)
        listing = registry.execute("list_windows", {})
        assert "notepad.exe" in listing.text.lower(), listing.text
        result = registry.execute("window_action", {"name": "блокнот", "action": "minimize"})
        assert result.ok, result.text
        hwnd = winapi.top_windows({p.pid for p in processes("notepad.exe")})[0]
        assert wait_for(lambda: winapi.user32.IsIconic(hwnd), 5)
        result = registry.execute("window_action", {"name": "notepad", "action": "focus"})
        assert result.ok, result.text
        assert wait_for(lambda: not winapi.user32.IsIconic(hwnd), 5)
    finally:
        kill_all("notepad.exe")


def test_find_files_on_desktop(registry):
    desktop = winapi.known_folder("Desktop")
    target = desktop / f"Отчёт Джарвиса {os.getpid()}.txt"
    target.write_text("x", encoding="utf-8")
    try:
        result = registry.execute("find_files", {"name": "отчёт джарвиса"})
        assert str(target) in result.text, result.text
    finally:
        target.unlink(missing_ok=True)


def test_calculate_and_weather(registry):
    assert registry.execute("calculate", {"expression": "2500*1,15"}).text == "2500*1,15 = 2 875"
    result = registry.execute("weather", {"city": "Москва"})
    print(result.text)
    assert result.ok and "Сейчас" in result.text or "не удалось получить погоду" in result.text
