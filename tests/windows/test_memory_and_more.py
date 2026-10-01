"""Сценарии, выделенный текст, снимок экрана для зрения, место на диске и брифинг — на настоящей Windows."""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("проверки только для Windows", allow_module_level=True)

import json  # noqa: E402

from jarvis import vision, winapi  # noqa: E402
from jarvis.tools import _pyautogui  # noqa: E402

from .conftest import kill_all, wait_for  # noqa: E402
from .test_pc_control import notepad_window, open_notepad, window_text  # noqa: E402


@pytest.fixture
def no_notepad():
    kill_all("notepad.exe")
    yield
    kill_all("notepad.exe")


def test_routine_opens_notepad_types_and_minimizes(registry, no_notepad):
    """Шаги сценария идут подряд без модели: текст должен попасть в только что открытый Блокнот."""
    steps = [
        {"tool": "open_app", "arguments": {"name": "блокнот"}},
        {"tool": "type_text", "arguments": {"text": "Сценарий Джарвиса работает"}},
        {"tool": "window_action", "arguments": {"name": "блокнот", "action": "minimize"}},
    ]
    saved = registry.execute("routine", {"action": "save", "name": "проверка блокнота",
                                         "steps": json.dumps(steps, ensure_ascii=False)})
    assert saved.ok, saved.text
    result = registry.execute("routine", {"action": "run", "name": "проверка блокнота"})
    print(result.text)
    assert result.ok and "с ошибками" not in result.text, result.text
    hwnd = notepad_window()
    assert hwnd, "окно Блокнота не появилось"
    assert wait_for(lambda: "Сценарий Джарвиса работает" in window_text(hwnd), 5), window_text(hwnd)
    assert wait_for(lambda: winapi.user32.IsIconic(hwnd), 5)
    registry.execute("routine", {"action": "delete", "name": "проверка блокнота"})


def test_selected_text_and_clipboard_is_restored(registry, no_notepad):
    hwnd = open_notepad(registry)
    assert registry.execute("type_text", {"text": "Выделенный текст для Джарвиса"}).ok
    assert wait_for(lambda: "Выделенный текст" in window_text(hwnd), 5), window_text(hwnd)
    winapi.set_clipboard_text("прежний буфер")
    assert registry.execute("hotkey", {"keys": "ctrl+a"}).ok
    result = registry.execute("clipboard", {"action": "selection"})
    assert result.ok and "Выделенный текст для Джарвиса" in result.text, result.text
    assert winapi.get_clipboard_text() == "прежний буфер"  # буфер обмена вернули как было


def test_screen_capture_for_vision(artifacts):
    image = vision.capture()
    width, height = _pyautogui().size()
    assert image.size == (width, height), (image.size, width, height)  # координаты снимка = координаты мыши
    data = vision.encode(image)
    assert data[:2] == b"\xff\xd8" and len(data) > 5_000
    crop = vision.preview(image, width // 2, height // 2)
    crop.save(artifacts / "vision_preview.png")
    assert crop.size == (360, 360)
    assert len(set(image.convert("L").resize((64, 48)).getdata())) > 3, "снимок экрана пустой"


def test_disk_usage_and_briefing(registry):
    result = registry.execute("disk_usage", {})
    print(result.text)
    assert result.ok and "Диск C:\\" in result.text and "Больше всего места" in result.text, result.text
    result = registry.execute("briefing", {})
    print(result.text)
    assert result.ok and ", сэр. Сегодня" in result.text, result.text
