"""Окно Джарвиса целиком на настоящей Windows: голосовая команда → модель → инструмент → подтверждение.

Модель здесь — простая заглушка (ответы предсказуемы), всё остальное настоящее: окно, Vosk,
Silero, микрофон (виртуальный кабель VB-CABLE), запуск и закрытие Блокнота, диалог «Да/Нет»
и щелчок мышью по кнопке, предложение помощи с ответом голосом, HUD поверх окон при свёрнутом
окне. Снимки экрана сохраняются в ci_artifacts.
"""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("проверки только для Windows", allow_module_level=True)

import threading  # noqa: E402
import time  # noqa: E402

import yaml  # noqa: E402

from jarvis.config import DEFAULTS, ROOT_DIR, Config, deep_merge  # noqa: E402
from jarvis.llm.base import AssistantTurn, LLMBackend, ToolCall  # noqa: E402
from jarvis.proactive import Suggestion  # noqa: E402

from .conftest import ENABLED, kill_all, processes, wait_for  # noqa: E402
from .test_voice import cable_devices, say  # noqa: E402


class ScriptedBackend(LLMBackend):
    """Заглушка модели: «открой блокнот» → open_app, «закрой блокнот» → close_app."""

    name = "ollama"
    title = "Тест"
    requests: list[str] = []

    @property
    def model(self) -> str:
        return "scripted"

    def stream_chat(self, system, messages, tools, allow_tools, cancel, on_text):
        last_user = [m for m in messages if m["role"] == "user"][-1]["content"].lower()
        if messages[-1]["role"] == "tool":
            text = "Готово, сэр."
            on_text(text)
            return AssistantTurn(text=text)
        self.requests.append(last_user)
        time.sleep(0.3)
        if allow_tools and "блокнот" in last_user and "закр" in last_user:
            return AssistantTurn(tool_calls=[ToolCall(name="close_app", arguments={"name": "блокнот"})])
        if allow_tools and "блокнот" in last_user:
            return AssistantTurn(tool_calls=[ToolCall(name="open_app", arguments={"name": "блокнот"})])
        text = f"Вы сказали: {last_user}."
        on_text(text)
        return AssistantTurn(text=text)


def grab(artifacts, name: str) -> None:
    from PIL import ImageGrab

    ImageGrab.grab(all_screens=True).save(artifacts / f"{name}.png")


def click_hud(app) -> bool:
    import pyautogui

    found = {}
    hud = app.ui.hud
    app.ui.post("call", lambda: found.update(xy=(hud.winfo_rootx() + 60, hud.winfo_rooty() + 16)))
    if not wait_for(lambda: "xy" in found, 5):
        return False
    pyautogui.click(*found["xy"])
    return True


def click_button(app, label: str) -> bool:
    import customtkinter as ctk
    import pyautogui

    from jarvis.ui.dialogs import ConfirmDialog

    found = {}

    def locate():
        stack = [w for w in app.ui.winfo_children() if isinstance(w, ConfirmDialog)]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if isinstance(widget, ctk.CTkButton) and widget.cget("text") == label:
                found["xy"] = (widget.winfo_rootx() + widget.winfo_width() // 2,
                               widget.winfo_rooty() + widget.winfo_height() // 2)

    app.ui.post("call", locate)
    if not wait_for(lambda: "xy" in found, 5):
        return False
    pyautogui.click(*found["xy"])
    return True


@pytest.mark.skipif(not ENABLED, reason="только в CI с JARVIS_WINDOWS_E2E=1")
def test_voice_command_opens_and_closes_notepad(monkeypatch, artifacts):
    playback, capture = cable_devices()
    if playback is None or capture is None:
        pytest.skip("виртуальный кабель VB-CABLE не установлен")
    import jarvis.app as app_module
    import main

    main.setup_logging()  # лог работы окна — в logs/jarvis.log (попадёт в артефакты CI)
    monkeypatch.setattr(app_module, "create_backend", lambda name, config: ScriptedBackend(config))
    raw = yaml.safe_load((ROOT_DIR / "config.yaml").read_text(encoding="utf-8"))
    config = Config(deep_merge(DEFAULTS, raw), path=None)
    config.set("voice.input_device", "CABLE Output")
    config.set("proactive.enabled", False)  # предложение покажем сами, без проверок диска и брифинга
    config.set("proactive.quiet_hours", "")  # CI может работать ночью — вопрос всё равно озвучиваем
    kill_all("notepad.exe")
    app = app_module.JarvisApp(config)
    result: dict = {}

    def scenario():
        try:
            assert wait_for(lambda: not app._loading, 180, 0.5), "голосовые модели не загрузились"
            time.sleep(2)
            result["voice"] = app.listener.available and app.listener.stream_open
            result["mic"] = app.listener.device_name
            grab(artifacts, "ui_1_ready")
            say(app.speaker, "Джарвис, открой блокнот.", playback)
            result["opened"] = bool(wait_for(lambda: processes("notepad.exe"), 25))
            wait_for(lambda: app._current is None and not app.speaker.busy, 20)
            time.sleep(1)
            grab(artifacts, "ui_2_notepad_opened")
            say(app.speaker, "Джарвис, закрой блокнот.", playback)
            result["confirm"] = bool(wait_for(lambda: app._pending_confirm is not None, 25))
            time.sleep(1.5)
            grab(artifacts, "ui_3_confirm_dialog")
            result["clicked"] = click_button(app, "Да, выполнить")
            result["closed"] = bool(wait_for(lambda: not processes("notepad.exe"), 20))
            wait_for(lambda: app._current is None and not app.speaker.busy, 20)
            time.sleep(1)
            grab(artifacts, "ui_4_done")

            # Джарвис сам предлагает помощь: карточка в чате, вопрос голосом, ответ голосом «да, давай»
            app._deliver_suggestion(Suggestion("test", "Сэр, это проверка. Открыть блокнот?", "открой блокнот"))
            result["asked"] = bool(wait_for(lambda: not app.speaker.busy, 30))
            time.sleep(0.3)
            say(app.speaker, "Да, давай.", playback)
            result["suggestion_yes"] = bool(wait_for(lambda: processes("notepad.exe"), 25))
            wait_for(lambda: app._current is None and not app.speaker.busy, 20)
            time.sleep(1)
            grab(artifacts, "ui_5_suggestion_accepted")
            kill_all("notepad.exe")

            # HUD: окно Джарвиса свёрнуто, а поверх других окон видно, что он делает
            app.ui.post("call", app.ui.iconify)
            time.sleep(1.5)
            app.submit_text("расскажи анекдот")
            result["hud"] = bool(wait_for(lambda: app.ui.hud is not None and app.ui.hud._shown, 10))
            time.sleep(0.3)
            grab(artifacts, "ui_6_hud")
            result["hud_click"] = click_hud(app)  # щелчок по HUD открывает окно, а HUD прячется
            result["hud_hidden"] = bool(wait_for(lambda: not app.ui.hud._shown, 10))
            wait_for(lambda: app._current is None and not app.speaker.busy, 20)
            time.sleep(0.5)
            grab(artifacts, "ui_7_restored")
        except BaseException as exc:  # noqa: BLE001 — результат проверяем в основном потоке
            result["error"] = repr(exc)
            grab(artifacts, "ui_error")
        finally:
            app.ui.post("call", app.ui._on_close)

    threading.Thread(target=scenario, daemon=True).start()
    app.run()
    kill_all("notepad.exe")
    print("Результат:", result, "Запросы к модели:", ScriptedBackend.requests)
    assert "error" not in result, result
    assert result["voice"], result
    assert result["opened"], (result, ScriptedBackend.requests)
    assert any("открой блокнот" in r for r in ScriptedBackend.requests), ScriptedBackend.requests
    assert result["confirm"] and result["clicked"] and result["closed"], result
    assert result["asked"] and result["suggestion_yes"], (result, ScriptedBackend.requests)
    assert result["hud"] and result["hud_click"] and result["hud_hidden"], result
