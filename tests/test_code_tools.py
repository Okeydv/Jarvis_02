"""Код и файлы, калькулятор, погода, буфер обмена, окна, напоминания."""

import sys
import threading
from datetime import datetime, timedelta

import pytest

from jarvis import tools, winapi
from jarvis.tools import TOOLS, ToolError, ToolRegistry, ToolServices, format_number, safe_eval


class Recorder:
    def __init__(self, answer=True):
        self.answer = answer
        self.asked = []

    def __call__(self, confirmation):
        self.asked.append(confirmation)
        return self.answer


@pytest.fixture
def code_dir(config, tmp_path, monkeypatch):
    folder = tmp_path / "Code"
    config.set("tools.code_folder", str(folder))
    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    monkeypatch.setattr(tools.Path, "home", staticmethod(lambda: tmp_path))  # папка пользователя
    return folder


def registry_with(config, answer=True, **kwargs):
    recorder = Recorder(answer)
    return ToolRegistry(ToolServices(config, confirm=recorder, **kwargs)), recorder


def test_write_read_and_overwrite(config, code_dir, monkeypatch):
    opened = []
    monkeypatch.setattr(tools, "open_in_editor", lambda path, cfg: opened.append(path) or "Visual Studio Code")
    registry, recorder = registry_with(config)
    code = "def factorial(n):\n    return 1 if n < 2 else n * factorial(n - 1)\n\nprint(factorial(10))\n"
    result = registry.execute("write_file", {"path": "factorial.py", "content": code})
    assert result.ok and "Visual Studio Code" in result.text and "5 строк" in result.text, result.text
    assert (code_dir / "factorial.py").read_text(encoding="utf-8") == code
    assert opened == [code_dir / "factorial.py"] and recorder.asked == []  # новый файл — без вопросов

    read = registry.execute("read_file", {"path": "factorial.py"})
    assert read.ok and "def factorial" in read.text

    recorder.answer = False  # перезапись — только с подтверждением
    result = registry.execute("write_file", {"path": "factorial.py", "content": "print('x')", "open": False})
    assert result.status == "cancelled" and "перезаписан" in recorder.asked[-1].text
    assert recorder.asked[-1].details == "print('x')"
    assert (code_dir / "factorial.py").read_text(encoding="utf-8") == code
    recorder.answer = True
    assert registry.execute("write_file", {"path": "factorial.py", "content": "print('x')", "open": False}).ok
    assert (code_dir / "factorial.py").read_text(encoding="utf-8") == "print('x')"


def test_write_file_to_known_folder_and_powershell_bom(config, code_dir, tmp_path):
    registry, _ = registry_with(config)
    assert registry.execute("write_file", {"path": "Рабочий стол/заметка.txt", "content": "привет", "open": False}).ok
    assert (tmp_path / "Desktop" / "заметка.txt").read_text(encoding="utf-8") == "привет"
    assert registry.execute("write_file", {"path": "run.ps1", "content": "Write-Output 'Привет'", "open": False}).ok
    assert (code_dir / "run.ps1").read_bytes().startswith(b"\xef\xbb\xbf")


def test_write_outside_home_needs_confirmation(config, code_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(tools.Path, "home", staticmethod(lambda: tmp_path / "home"))
    registry, recorder = registry_with(config, answer=False)
    result = registry.execute("write_file", {"path": str(tmp_path / "elsewhere" / "x.txt"), "content": "x",
                                             "open": False})
    assert result.status == "cancelled" and "вне вашей папки" in recorder.asked[-1].text


def test_read_file_rejects_binary_and_cuts_long_text(config, code_dir):
    code_dir.mkdir(parents=True)
    (code_dir / "image.bin").write_bytes(b"\x89PNG\x00\x00binary")
    (code_dir / "long.txt").write_text("я" * 20000, encoding="utf-8")
    registry, _ = registry_with(config)
    assert "двоичный" in registry.execute("read_file", {"path": "image.bin"}).text
    text = registry.execute("read_file", {"path": "long.txt"}).text
    assert "показаны первые 12000 из 20000" in text and len(text) < 13000
    assert registry.execute("read_file", {"path": "нет.txt"}).status == "error"


def test_open_file_never_runs_scripts(config, code_dir, monkeypatch):
    code_dir.mkdir(parents=True)
    (code_dir / "game.py").write_text("print(1)", encoding="utf-8")
    (code_dir / "photo.png").write_bytes(b"png")
    edited, started = [], []
    monkeypatch.setattr(tools, "open_in_editor", lambda path, cfg: edited.append(path.name) or "Блокнот")
    monkeypatch.setattr(tools.os, "startfile", lambda path: started.append(path), raising=False)
    registry, _ = registry_with(config)
    assert "в редакторе" in registry.execute("open_file", {"path": "game.py"}).text
    assert registry.execute("open_file", {"path": "photo.png"}).ok
    assert edited == ["game.py"] and started == [str(code_dir / "photo.png")]


def test_run_python_needs_confirmation_and_returns_output(config, code_dir):
    registry, recorder = registry_with(config, answer=False)
    code = "import sys\nprint('Привет из Python', sys.version_info[0])\nprint(2 ** 10)"
    result = registry.execute("run_python", {"code": code})
    assert result.status == "cancelled" and recorder.asked[-1].details == code
    assert recorder.asked[-1].title == "Запуск кода Python"

    recorder.answer = True
    result = registry.execute("run_python", {"code": code})
    assert result.ok and "Код завершения 0" in result.text, result.text
    assert "Привет из Python 3" in result.text and "1024" in result.text

    result = registry.execute("run_python", {"code": "raise ValueError('ошибка в программе')"})
    assert "Код завершения 1" in result.text and "ошибка в программе" in result.text  # модель увидит ошибку


def test_run_python_file_and_timeout(config, code_dir):
    code_dir.mkdir(parents=True)
    (code_dir / "hello.py").write_text("print('файл работает')", encoding="utf-8")
    registry, recorder = registry_with(config)
    result = registry.execute("run_python", {"path": "hello.py"})
    assert "файл работает" in result.text and "hello.py" in recorder.asked[-1].text
    config.set("tools.code_timeout", 1)
    result = registry.execute("run_python", {"code": "import time\ntime.sleep(5)"})
    assert result.status == "error" and "дольше 1 с" in result.text
    assert registry.execute("run_python", {}).status == "error"  # ни кода, ни файла


def test_code_execution_can_be_disabled(config):
    config.set("tools.allow_code", False)
    registry, _ = registry_with(config)
    assert "run_python" not in [s["name"] for s in registry.schemas()]
    assert registry.execute("run_python", {"code": "print(1)"}).status == "error"


def test_find_files(config, tmp_path, monkeypatch):
    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    (tmp_path / "Documents" / "Работа").mkdir(parents=True)
    (tmp_path / "Documents" / "Работа" / "Отчёт за май.docx").write_text("x", encoding="utf-8")
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "отчёт.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "Downloads" / "node_modules").mkdir()
    (tmp_path / "Downloads" / "node_modules" / "отчёт.js").write_text("x", encoding="utf-8")
    registry, _ = registry_with(config)
    text = registry.execute("find_files", {"name": "ОТЧЁТ"}).text
    assert "Отчёт за май.docx" in text and "отчёт.pdf" in text and "node_modules" not in text
    assert "Ничего не найдено" in registry.execute("find_files", {"name": "космос"}).text
    assert "отчёт.pdf" in registry.execute("find_files", {"name": ".pdf", "folder": "Загрузки"}).text


@pytest.mark.parametrize("expression, expected", [
    ("2+2*2", "6"), ("2^10", "1 024"), ("(120+80)*1,2", "240"), ("15% * 3000", "450"),
    ("sqrt(16)", "4"), ("10/4", "2,5"), ("-3 ** 2", "-9"), ("round(pi, 2)", "3,14"),
])
def test_calculate(expression, expected):
    assert format_number(safe_eval(expression)) == expected


@pytest.mark.parametrize("expression", ["__import__('os').system('dir')", "open('x')", "9**9**9", "1/0",
                                        "factorial(5000)", "x + 1", "2 +"])
def test_calculate_rejects_unsafe_or_invalid(expression):
    with pytest.raises(ToolError):
        safe_eval(expression)


WEATHER = {
    "current_condition": [{"temp_C": "12", "FeelsLikeC": "9", "windspeedKmph": "18", "humidity": "71",
                           "lang_ru": [{"value": "Переменная облачность"}]}],
    "nearest_area": [{"areaName": [{"value": "Moscow"}]}],
    "weather": [
        {"mintempC": "7", "maxtempC": "14", "hourly": [{"chanceofrain": "10", "lang_ru": [{"value": "Ясно"}]},
                                                       {"chanceofrain": "40", "lang_ru": [{"value": "Дождь"}]}]},
        {"mintempC": "-2", "maxtempC": "5", "hourly": [{"chanceofrain": "0", "lang_ru": [{"value": "Снег"}]}]},
    ],
}


def test_weather(config, monkeypatch):
    import httpx

    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append((url, params))
        return httpx.Response(200, json=WEATHER, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    registry, _ = registry_with(config)
    text = registry.execute("weather", {"city": "Москва"}).text
    assert text.startswith("Погода: Москва. Сейчас +12°C (ощущается как +9°C), переменная облачность")
    assert "ветер 5 м/с" in text and "Сегодня от +7 до +14°C, дождь, вероятность дождя до 40%" in text
    assert "Завтра от -2 до +5°C, снег" in text
    assert calls[0][0].endswith("/%D0%9C%D0%BE%D1%81%D0%BA%D0%B2%D0%B0") and calls[0][1]["lang"] == "ru"

    def offline(*args, **kwargs):
        raise httpx.ConnectError("нет сети")

    monkeypatch.setattr(httpx, "get", offline)
    result = registry.execute("weather", {})
    assert result.status == "error" and "не удалось получить погоду" in result.text


def test_clipboard(config, monkeypatch):
    store = {"text": "скопированный текст"}
    monkeypatch.setattr(winapi, "get_clipboard_text", lambda: store["text"])
    monkeypatch.setattr(winapi, "set_clipboard_text", lambda text: store.update(text=text))
    registry, _ = registry_with(config)
    assert registry.execute("clipboard", {"action": "get"}).text.endswith("скопированный текст")
    assert registry.execute("clipboard", {"action": "set", "text": "новый"}).ok and store["text"] == "новый"
    assert TOOLS["clipboard"].is_quick({"action": "set"}) and not TOOLS["clipboard"].is_quick({"action": "get"})


def test_window_action(config, monkeypatch):
    windows = [(101, "Документ — Блокнот", "notepad.exe"), (202, "YouTube — Google Chrome", "chrome.exe")]
    shown, activated = [], []
    monkeypatch.setattr(winapi, "_require_windows", lambda: None)
    monkeypatch.setattr(tools, "_app_windows", lambda: windows)
    monkeypatch.setattr(winapi, "show_window", lambda hwnd, command: shown.append((hwnd, command)) or True)
    monkeypatch.setattr(winapi, "activate_window", lambda hwnd, timeout=1.0: activated.append(hwnd) or True)
    registry, _ = registry_with(config)
    assert registry.execute("window_action", {"name": "хром", "action": "focus"}).text == \
        "Переключился на окно «YouTube — Google Chrome»."
    assert registry.execute("window_action", {"name": "блокнот", "action": "minimize"}).ok
    assert activated == [202] and shown == [(101, winapi.SW_MINIMIZE)]
    assert registry.execute("window_action", {"name": "фотошоп", "action": "focus"}).status == "error"
    text = registry.execute("list_windows", {}).text
    assert "Документ — Блокнот (notepad.exe)" in text


def test_timer_at_time(config):
    notified = threading.Event()
    registry, _ = registry_with(config, notify=lambda title, text: notified.set())
    soon = datetime.now() + timedelta(minutes=2)
    text = registry.execute("timer", {"at": f"{soon:%H:%M}", "label": "позвонить маме"}).text
    assert text == f"Напоминание «позвонить маме» поставлено на {soon:%H:%M}."
    past = datetime.now() - timedelta(minutes=5)
    assert "завтра" in registry.execute("timer", {"at": f"{past:%H:%M}"}).text
    assert registry.execute("timer", {"at": "25:99"}).status == "error"
    assert registry.execute("timer", {}).status == "error"
    for timer in registry.services.timers:
        timer.cancel()


@pytest.mark.skipif(sys.platform == "win32", reason="на Windows буфер обмена настоящий")
def test_clipboard_needs_windows(config):
    registry, _ = registry_with(config)
    assert registry.execute("clipboard", {"action": "get"}).status == "error"
