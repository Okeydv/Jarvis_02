"""Управление компьютером на настоящей Windows: инструменты Джарвиса делают то, что обещают."""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("проверки только для Windows", allow_module_level=True)

import ctypes  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
from ctypes import wintypes  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from jarvis import winapi  # noqa: E402
from jarvis.hotkey import GlobalHotkey  # noqa: E402
from jarvis.tools import list_shortcuts, list_start_apps  # noqa: E402

from .conftest import kill_all, processes, wait_for  # noqa: E402

WM_GETTEXT, WM_GETTEXTLENGTH = 0x000D, 0x000E


def window_text(hwnd: int) -> str:
    """Текст из поля ввода окна (классический или новый Блокнот)."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = ctypes.c_ssize_t
    user32.EnumChildWindows.argtypes = [wintypes.HWND, winapi.WNDENUMPROC, wintypes.LPARAM]
    texts = []

    def callback(child, _lparam):
        if winapi.window_class(child) in ("Edit", "RichEditD2DPT", "RICHEDIT50W"):
            length = user32.SendMessageW(child, WM_GETTEXTLENGTH, 0, 0)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.SendMessageW(child, WM_GETTEXT, length + 1, ctypes.addressof(buffer))
            texts.append(buffer.value)
        return True

    user32.EnumChildWindows(hwnd, winapi.WNDENUMPROC(callback), 0)
    return "\n".join(texts)


def notepad_window() -> int:
    pids = {p.pid for p in processes("notepad.exe")}
    windows = winapi.top_windows(pids) if pids else []
    return windows[0] if windows else 0


@pytest.fixture
def no_notepad():
    kill_all("notepad.exe")
    yield
    kill_all("notepad.exe")


def open_notepad(registry) -> int:
    result = registry.execute("open_app", {"name": "блокнот"})
    assert result.ok, result.text
    hwnd = wait_for(notepad_window, 15)
    assert hwnd, "окно Блокнота не появилось"
    assert winapi.activate_window(hwnd, timeout=3) or wait_for(lambda: winapi.foreground_window() == hwnd, 3)
    time.sleep(0.5)
    return hwnd


def test_open_notepad_type_russian_text_and_hotkeys(registry, confirmer, no_notepad):
    hwnd = open_notepad(registry)
    text = "Привет, сэр! Это Джарвис — 2026 год, ёжик 🙂"
    result = registry.execute("type_text", {"text": text})
    assert result.ok, result.text
    assert wait_for(lambda: window_text(hwnd) == text, 5), f"в Блокноте: {window_text(hwnd)!r}"

    # Сочетания клавиш: выделить всё и напечатать поверх
    assert registry.execute("hotkey", {"keys": "ctrl+a"}).ok
    assert registry.execute("type_text", {"text": "Замена"}).ok
    assert wait_for(lambda: window_text(hwnd) == "Замена", 5), f"в Блокноте: {window_text(hwnd)!r}"
    # «ctrl+ф» — та же физическая клавиша, что Ctrl+A (команда, сказанная в русской раскладке)
    assert registry.execute("hotkey", {"keys": "ctrl+ф"}).ok
    assert registry.execute("hotkey", {"keys": "delete"}).ok
    assert wait_for(lambda: window_text(hwnd) == "", 5), f"в Блокноте: {window_text(hwnd)!r}"

    result = registry.execute("close_app", {"name": "блокнот", "force": True})
    assert result.ok and "принудительно" in result.text, result.text
    assert "ПРИНУДИТЕЛЬНО" in confirmer.asked[-1].text
    assert wait_for(lambda: not processes("notepad.exe"), 10)


def test_close_app_only_after_confirmation(registry, confirmer, no_notepad):
    hwnd = open_notepad(registry)
    confirmer.answer = False
    result = registry.execute("close_app", {"name": "блокнот"})
    assert result.status == "cancelled", result.text
    asked = confirmer.asked[-1]
    assert "блокнот" in (asked.title + asked.text).lower() and "notepad.exe" in asked.text.lower()
    time.sleep(1)
    assert winapi.is_window(hwnd)  # «Нет» — Блокнот остался открытым
    confirmer.answer = True
    result = registry.execute("close_app", {"name": "notepad"})
    assert result.ok and "закрыто" in result.text, result.text
    assert wait_for(lambda: not processes("notepad.exe"), 10)


def test_alt_f4_asks_before_closing_window(registry, confirmer, no_notepad):
    hwnd = open_notepad(registry)
    confirmer.answer = False
    result = registry.execute("hotkey", {"keys": "alt+f4"})
    assert result.status == "cancelled", result.text
    assert winapi.window_title(hwnd) in confirmer.asked[-1].text
    time.sleep(1)
    assert winapi.is_window(hwnd)
    confirmer.answer = True
    assert winapi.activate_window(hwnd, timeout=3)
    result = registry.execute("hotkey", {"keys": "alt+f4"})
    assert result.ok, result.text
    assert wait_for(lambda: not winapi.is_window(hwnd), 10)


def test_unsaved_changes_are_not_lost_silently(registry, confirmer, no_notepad):
    hwnd = open_notepad(registry)
    assert registry.execute("type_text", {"text": "несохранённый текст"}).ok
    assert wait_for(lambda: window_text(hwnd), 5)
    result = registry.execute("close_app", {"name": "блокнот"})
    # Классический Блокнот спрашивает о сохранении — Джарвис честно говорит, что окно не закрылось
    assert result.ok and ("не закрылось" in result.text or "закрыто" in result.text), result.text
    if processes("notepad.exe"):
        assert "не закрылось" in result.text
        result = registry.execute("close_app", {"name": "блокнот", "force": True})
        assert result.ok and "принудительно" in result.text, result.text
    assert wait_for(lambda: not processes("notepad.exe"), 10)


def test_screenshot_note_and_folders(registry, artifacts):
    result = registry.execute("screenshot", {})
    assert result.ok, result.text
    path = Path(result.text.split(": ", 1)[1])
    assert path.is_file() and path.stat().st_size > 10_000
    (artifacts / "tool_screenshot.png").write_bytes(path.read_bytes())

    marker = f"проверка заметки {datetime.now():%H%M%S}"
    assert registry.execute("note", {"text": marker}).ok
    notes = winapi.known_folder("Documents") / "Jarvis" / "notes.txt"
    assert marker in notes.read_text(encoding="utf-8")

    name = f"Джарвис тест {os.getpid()}"
    result = registry.execute("create_folder", {"path": f"Рабочий стол/{name}"})
    assert result.ok, result.text
    folder = winapi.known_folder("Desktop") / name
    assert folder.is_dir()
    (folder / "отчёт.txt").write_text("x", encoding="utf-8")
    listing = registry.execute("list_folder", {"path": str(folder)})
    assert listing.ok and "отчёт.txt" in listing.text

    before = set(winapi.top_windows(class_name="CabinetWClass"))
    result = registry.execute("open_folder", {"path": "Загрузки"})
    assert result.ok, result.text
    assert wait_for(lambda: set(winapi.top_windows(class_name="CabinetWClass")) - before, 10), "окно Проводника не открылось"
    result = registry.execute("close_app", {"name": "проводник"})
    assert result.ok, result.text
    assert wait_for(lambda: not winapi.top_windows(class_name="CabinetWClass"), 10)
    for item in folder.iterdir():
        item.unlink()
    folder.rmdir()


def test_known_folders_exist():
    for name in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
        path = winapi.known_folder(name)
        assert path.is_absolute(), name
    assert winapi.known_folder("Documents").is_dir()


def test_system_info_and_datetime(registry):
    info = registry.execute("system_info", {})
    assert info.ok and "Процессор" in info.text and "Оперативная память" in info.text, info.text
    assert registry.execute("get_datetime", {}).text.startswith("Сейчас")


def test_volume_controls(registry):
    import sounddevice as sd

    outputs = [d for d in sd.query_devices() if d["max_output_channels"] > 0]
    result = registry.execute("set_volume", {"level": 30})
    if not result.ok:
        # Без звуковой карты — понятная ошибка, программа не падает
        assert "устройство вывода" in result.text, result.text
        pytest.skip(f"нет устройства вывода звука ({len(outputs)} в PortAudio): {result.text}")
    from jarvis.tools import _endpoint_volume

    volume = _endpoint_volume()
    assert round(volume.GetMasterVolumeLevelScalar() * 100) == 30
    assert registry.execute("change_volume", {"delta": 15}).text == "Громкость: было 30%, стало 45%."
    assert registry.execute("mute", {"on": True}).ok and volume.GetMute()
    assert registry.execute("change_volume", {"delta": -5}).ok and not volume.GetMute()
    assert round(volume.GetMasterVolumeLevelScalar() * 100) == 40


def test_brightness_reports_clear_error_on_desktop(registry):
    result = registry.execute("set_brightness", {"level": 70})
    assert result.ok or "яркость" in result.text, result.text


def test_powershell_and_scheduled_shutdown(config, registry, confirmer):
    config.set("tools.allow_powershell", True)
    command = "Get-Date -Format yyyy; Write-Output 'Привет'"
    result = registry.execute("run_powershell", {"command": command})
    assert result.ok and str(datetime.now().year) in result.text and "Привет" in result.text, result.text
    assert confirmer.asked[-1].details == command  # команда показана целиком

    config.set("tools.shutdown_delay", 900)
    result = registry.execute("shutdown_pc", {})
    assert result.ok and "через 900 с" in result.text, result.text
    assert confirmer.asked[-1].title == "Выключение компьютера"
    cancel = registry.execute("cancel_shutdown", {})
    assert cancel.ok and "отменены" in cancel.text, cancel.text


def test_global_hotkey_ctrl_alt_j():
    pressed = threading.Event()
    hotkey = GlobalHotkey("ctrl+alt+j", pressed.set)
    assert hotkey.start(), hotkey.error
    try:
        from jarvis.tools import _pyautogui

        _pyautogui().hotkey("ctrl", "alt", "j")
        assert pressed.wait(5), "Ctrl+Alt+J не дошло до Джарвиса"
    finally:
        hotkey.stop()


def test_media_key_and_timer_notification(registry, notifications):
    assert registry.execute("media", {"action": "play_pause"}).ok
    assert registry.execute("timer", {"minutes": 0.05, "label": "чай"}).ok
    assert wait_for(lambda: notifications, 10)
    assert "чай" in notifications[0][1]


def test_start_menu_is_searchable():
    shortcuts = list_shortcuts()
    apps = list_start_apps()
    assert len(shortcuts) + len(apps) > 5, (shortcuts[:5], apps[:5])


def test_open_url_in_browser(registry):
    browsers = ("msedge.exe", "chrome.exe", "firefox.exe")
    for name in browsers:
        kill_all(name)
    before = {p.pid for name in browsers for p in processes(name)}
    result = registry.execute("open_url", {"url": "example.com"})
    assert result.ok, result.text
    started = wait_for(lambda: {p.pid for name in browsers for p in processes(name)} - before, 20)
    for name in browsers:
        kill_all(name)
    assert started, "браузер не запустился"
