"""Контроллер: связывает окно, агента, голос, инструменты и горячую клавишу.

Потоки: главный — окно (Tk); agent — запросы к модели и инструменты; stt — микрофон;
tts-synth / tts-play — синтез и воспроизведение; hotkey — Ctrl+Alt+J; loader — загрузка
моделей при старте. С окном все общаются только через MainWindow.post (очередь).
"""

from __future__ import annotations

import itertools
import json
import logging
import queue
import threading
import time
from datetime import datetime

from . import winapi
from .agent import Agent
from .config import Config
from .hotkey import GlobalHotkey
from .llm import BACKEND_TITLES, LLMBackend, LLMError, create_backend
from .llm.base import ToolCall
from .stt import Listener, VoiceError
from .text_utils import SentenceSplitter, contains_stop_word, find_wake_word, is_stop_phrase
from .tools import Confirmation, ToolRegistry, ToolResult, ToolServices
from .tts import Speaker, SpeechError
from .ui import MainWindow

log = logging.getLogger(__name__)


class ConfirmRequest:
    """Запрос подтверждения: агент ждёт ответа, окно показывает диалог."""

    def __init__(self, confirmation: Confirmation, timeout: float):
        self.confirmation = confirmation
        self.deadline = time.monotonic() + timeout
        self.result = False
        self.reason = ""
        self._event = threading.Event()
        self._lock = threading.Lock()

    @property
    def done(self) -> bool:
        return self._event.is_set()

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def resolve(self, value: bool, reason: str = "") -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self.result = bool(value)
            self.reason = reason
            self._event.set()
            return True

    def wait(self) -> bool:
        self._event.wait(self.remaining())
        self.resolve(False, "timeout")
        return self.result


class JarvisApp:
    def __init__(self, config: Config):
        self.config = config
        self.focus = winapi.FocusTracker()
        self.speak_enabled = bool(config.get("voice.speak_replies", True))
        self.jarvis_mode = bool(config.get("voice.jarvis_mode", False))
        self.backend_name = str(config.get("llm.backend", "ollama"))

        self._backends: dict[str, LLMBackend] = {}
        self._backend_lock = threading.Lock()
        self._requests: queue.Queue = queue.Queue()
        self._request_ids = itertools.count(1)
        self._current: tuple[int, threading.Event] | None = None
        self._pending_confirm: ConfirmRequest | None = None
        self._splitter = SentenceSplitter()
        self._streamed = False
        self._agent_state = "idle"
        self._loading = True
        self._loading_text = ""
        self._listening = False
        self._speaking = False
        self._closing = False

        self.ui = MainWindow(self)
        self.speaker = Speaker(config, on_speaking=self._on_speaking, on_error=self._on_voice_error)
        self.listener = Listener(config, on_partial=self._on_partial, on_final=self._on_final,
                                 on_listening=self._on_listening, on_error=self._on_voice_error)
        self.services = ToolServices(config, confirm=self.ask_confirmation, notify=self.notify, focus=self.focus)
        self.registry = ToolRegistry(self.services)
        self.agent = Agent(config, self.registry, self._backend, self)
        self.hotkey = GlobalHotkey(str(config.get("hotkey", "ctrl+alt+j")), self._on_hotkey)

    # ═══ запуск и остановка ═══
    def run(self) -> None:
        self.ui.post("settings", self.jarvis_mode, self.speak_enabled)
        self.ui.post("backend", self.backend_name, self._model_name(self.backend_name))
        self.ui.post("voice_available", False)
        for error in self.config.load_errors:
            self.ui.post("error", error, "")
        self._refresh_state()
        threading.Thread(target=self._agent_worker, name="agent", daemon=True).start()
        threading.Thread(target=self._load_voice, name="loader", daemon=True).start()
        threading.Thread(target=self._check_backend, args=(self.backend_name,), name="check", daemon=True).start()
        if not self.hotkey.start():
            self.ui.post("error", f"Глобальная горячая клавиша не работает: {self.hotkey.error}", "")
        self.ui.mainloop()

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.stop()
        self._requests.put(None)
        self.hotkey.stop()
        self.listener.shutdown()
        for timer in self.services.timers:
            timer.cancel()
        for backend in list(self._backends.values()):
            backend.close()
        log.info("Джарвис завершает работу")

    # ═══ состояние ═══
    def _compute_state(self) -> str:
        if self._agent_state == "executing":
            return "executing"
        if self._speaking:
            return "speaking"
        if self._agent_state == "thinking":
            return "thinking"
        if self._listening:
            return "listening"
        if self._loading:
            return "loading"
        return "idle"

    def _refresh_state(self) -> None:
        state = self._compute_state()
        subtitle = ""
        if state == "loading":
            subtitle = self._loading_text
        elif state == "idle" and self.jarvis_mode and self.listener.available:
            subtitle = "скажите «Джарвис, …»"
        self.ui.post("state", state, subtitle)

    def _set_loading(self, text: str) -> None:
        self._loading_text = text
        self._refresh_state()

    # ═══ загрузка моделей ═══
    def _load_voice(self) -> None:
        voice_ok = False
        self._set_loading("распознавание речи")
        try:
            self.listener.load()
            voice_ok = True
        except VoiceError as exc:
            self.ui.post("error", exc.message, exc.hint)
        except Exception as exc:
            log.exception("Сбой загрузки Vosk")
            self.ui.post("error", f"Не удалось запустить распознавание речи: {exc}", "")
        if not voice_ok:
            self.jarvis_mode = False  # без микрофона режим недоступен (в config.yaml не пишем)
        self.ui.post("voice_available", voice_ok)
        self.ui.post("settings", self.jarvis_mode, self.speak_enabled)

        self._set_loading("синтез речи")
        try:
            self.speaker.load(status=self._set_loading)
        except SpeechError as exc:
            self.ui.post("error", exc.message, exc.hint)
        except Exception as exc:
            log.exception("Сбой загрузки Silero")
            self.ui.post("error", f"Не удалось запустить синтез речи: {exc}", "")

        self._loading = False
        combo = str(self.config.get("hotkey", "ctrl+alt+j")).upper()
        if voice_ok and self.jarvis_mode:
            self.listener.set_continuous(True)
        if voice_ok:
            self.ui.post("system", f"Джарвис готов. Нажмите {combo} или «Микрофон» и говорите, либо напишите команду. "
                                   "В режиме «Джарвис» начинайте фразу со слова «Джарвис».")
        else:
            self.ui.post("system", "Джарвис готов к текстовым командам.")
        if not self.speaker.available:
            self.ui.post("system", "Озвучка ответов недоступна — ответы будут только в чате.")
        self._refresh_state()

    # ═══ бэкенды ═══
    def _model_name(self, name: str) -> str:
        return str(self.config.get(f"{name}.model", ""))

    def _get_backend(self, name: str) -> LLMBackend:
        with self._backend_lock:
            backend = self._backends.get(name)
            if backend is None:
                if name != "ollama":
                    self.config.reload_env()  # ключ могли дописать в .env без перезапуска
                backend = create_backend(name, self.config)
                self._backends[name] = backend
            return backend

    def _backend(self) -> LLMBackend:
        return self._get_backend(self.backend_name)

    def _check_backend(self, name: str) -> None:
        try:
            backend = self._get_backend(name)
            backend.check()
            if name == self.backend_name:
                self.ui.post("system", f"{BACKEND_TITLES[name]}: модель {backend.model} на связи.")
            system = str(self.config.get("system_prompt") or "").strip()
            backend.warmup(system, self.registry.schemas())
        except LLMError as exc:
            if name == self.backend_name:
                self.ui.post("error", exc.message, exc.hint)
        except Exception as exc:
            log.exception("Проверка бэкенда %s", name)
            if name == self.backend_name:
                self.ui.post("error", f"{BACKEND_TITLES.get(name, name)}: {type(exc).__name__}: {exc}", "")

    # ═══ действия из окна ═══
    def submit_text(self, text: str, source: str = "text") -> None:
        text = (text or "").strip()
        if not text:
            return
        if is_stop_phrase(text, self.config.get("voice.stop_words", []), self.config.get("voice.wake_words", [])):
            self.stop()
            return
        if self._pending_confirm is not None:
            # Подтверждение — только кнопками: случайная фраза не должна ни подтвердить, ни отменить действие.
            self.ui.post("system", "Сначала ответьте «Да» или «Нет» в окне подтверждения (или нажмите «Стоп»).")
            return
        self.stop(keep_listening=True)  # новый запрос прерывает текущий
        request_id = next(self._request_ids)
        cancel = threading.Event()
        self.ui.post("user", request_id, text, source)
        self._requests.put((request_id, text, cancel))

    def stop(self, keep_listening: bool = False) -> None:
        """«Стоп»: прервать генерацию, речь и ожидание подтверждения."""
        current = self._current
        if current is not None:
            current[1].set()
        confirm = self._pending_confirm
        if confirm is not None:
            confirm.resolve(False, "stop")
        self.speaker.stop()
        if not keep_listening:
            self.listener.cancel_command()
        self._refresh_state()

    def toggle_mic(self) -> None:
        if not self.listener.available:
            self.ui.post("error", "Голосовой ввод недоступен.",
                         "Нужны модель Vosk (python download_models.py) и микрофон. Текстовый режим работает.")
            return
        if self.listener.command_active:
            self.listener.cancel_command()
            return
        self.stop()
        self.listener.listen_command()
        self.speaker.earcon("start")

    def set_jarvis_mode(self, enabled: bool) -> None:
        if enabled and not self.listener.available:
            self.ui.post("settings", False, self.speak_enabled)
            self.ui.post("error", "Режим «Джарвис» недоступен без микрофона и модели Vosk.", "")
            return
        self.jarvis_mode = enabled
        self.config.set("voice.jarvis_mode", enabled, persist=True)
        self.listener.set_continuous(enabled)
        self.ui.post("system", "Режим «Джарвис» включён: начинайте команду со слова «Джарвис»." if enabled
                     else "Режим «Джарвис» выключен.")
        self._refresh_state()

    def set_speak(self, enabled: bool) -> None:
        self.speak_enabled = enabled
        self.config.set("voice.speak_replies", enabled, persist=True)
        if not enabled:
            self.speaker.stop()

    def set_backend(self, name: str) -> None:
        if name == self.backend_name:
            return
        self.backend_name = name
        self.config.set("llm.backend", name, persist=True)
        self.ui.post("backend", name, self._model_name(name))
        self.ui.post("system", f"Бэкенд: {BACKEND_TITLES.get(name, name)} ({self._model_name(name)}).")
        threading.Thread(target=self._check_backend, args=(name,), name="check", daemon=True).start()

    def new_dialog(self) -> None:
        self.stop()
        self.agent.reset()
        self.ui.post("clear")
        self.ui.post("system", "Новый диалог: история очищена.")

    # ═══ агент ═══
    def _agent_worker(self) -> None:
        while True:
            item = self._requests.get()
            if item is None:
                return
            request_id, text, cancel = item
            if cancel.is_set():
                continue
            self._current = (request_id, cancel)
            self._splitter = SentenceSplitter()
            self._streamed = False
            try:
                outcome = self.agent.run(text, cancel)
                if cancel.is_set():
                    self.ui.post("assistant_end", request_id, " (прервано)" if self._streamed else "")
                else:
                    self.ui.post("assistant_end", request_id, "")
                    if not outcome.failed and not self._streamed:
                        if outcome.tools_used and not outcome.tool_failures:
                            reply = "Готово, сэр."
                        elif outcome.tools_used:
                            reply = "Сэр, не всё прошло гладко — подробности в журнале действий."
                        else:
                            reply = "Простите, сэр, модель не дала ответа."
                        self.ui.post("assistant_message", request_id, reply)
                        self._speak(reply, cancel)
            except Exception as exc:
                log.exception("Сбой обработки запроса")
                self.ui.post("error", f"Непредвиденная ошибка: {type(exc).__name__}: {exc}", "")
            finally:
                self._current = None
                self._agent_state = "idle"
                self._refresh_state()

    def _speak(self, text: str, cancel: threading.Event | None = None) -> None:
        if self.speak_enabled and self.speaker.available and not (cancel and cancel.is_set()):
            self.speaker.say(text)

    # ─── события агента (поток agent) ───
    def on_state(self, state: str) -> None:
        self._agent_state = state
        self._refresh_state()

    def on_text(self, delta: str) -> None:
        current = self._current
        if current is None or current[1].is_set():
            return
        self._streamed = True
        self.ui.post("assistant_delta", current[0], delta)
        if self.speak_enabled and self.speaker.available:
            for sentence in self._splitter.feed(delta):
                self.speaker.say(sentence)

    def on_turn_end(self) -> None:
        current = self._current
        sentences = self._splitter.flush()
        if current is None or current[1].is_set():
            return
        for sentence in sentences:
            self._speak(sentence, current[1])
        self.ui.post("assistant_break", current[0])

    def on_tool_start(self, call: ToolCall) -> None:
        arguments = json.dumps(call.arguments, ensure_ascii=False)
        if len(arguments) > 300:
            arguments = arguments[:300] + "…"
        self.ui.post("log_start", datetime.now().strftime("%H:%M:%S"), call.name, arguments)

    def on_tool_result(self, call: ToolCall, result: ToolResult) -> None:
        text = result.text if len(result.text) <= 600 else result.text[:600] + "…"
        self.ui.post("log_result", result.status, text)

    def on_error(self, message: str, hint: str = "") -> None:
        self.ui.post("error", message, hint)
        self._speak("Сэр, возникла ошибка. Подробности в окне.")

    # ═══ подтверждения и уведомления (вызываются инструментами) ═══
    def ask_confirmation(self, confirmation: Confirmation) -> bool:
        current = self._current
        if current is not None and current[1].is_set():
            return False
        request = ConfirmRequest(confirmation, float(self.config.get("tools.confirm_timeout", 20)))
        self._pending_confirm = request
        try:
            self.ui.post("confirm", request)
            self._speak(f"Сэр, требуется подтверждение: {confirmation.title}.")
            return request.wait()
        finally:
            self._pending_confirm = None
            if request.reason == "timeout":
                self.ui.post("system", f"Подтверждение «{confirmation.title}» не получено за отведённое время — действие отменено.")

    def notify(self, title: str, text: str) -> None:
        winapi.show_toast(f"Джарвис — {title}", text)
        self.ui.post("system", f"⏰ {text}")
        self._speak(text)

    # ═══ голос ═══
    def _on_hotkey(self) -> None:  # поток hotkey
        self.toggle_mic()

    def _on_listening(self, active: bool) -> None:
        self._listening = active
        self.ui.post("mic", active)
        if not active:
            self.ui.post("heard", "")
        self._refresh_state()

    def _on_partial(self, text: str) -> None:
        self.ui.post("heard", text)
        busy = self._current is not None or self._speaking or self.speaker.busy
        if (text and busy and contains_stop_word(text, self.config.get("voice.stop_words", []))
                and not self.speaker.is_echo(text)):
            self.stop()
            self.listener.reset()

    def _on_final(self, text: str, mode: str) -> None:
        self.ui.post("heard", "")
        if mode == "command":
            self.submit_text(text, "voice")
            return
        # Режим «Джарвис». Микрофон слышит и голос самого Джарвиса, поэтому во время речи
        # фразы, похожие на только что сказанное, отбрасываются как эхо.
        speaking = self._speaking or self.speaker.busy
        if speaking and self.speaker.is_echo(text):
            return
        found, command = find_wake_word(text, self.config.get("voice.wake_words", ["джарвис"]))
        if not found:
            if speaking and contains_stop_word(text, self.config.get("voice.stop_words", [])):
                self.stop()
            return
        if command:
            self.submit_text(command, "voice")  # прерывает текущий ответ
        else:
            self.stop(keep_listening=True)
            self.listener.listen_command(float(self.config.get("voice.follow_up_seconds", 6)))
            self.speaker.earcon("start")

    def _on_speaking(self, speaking: bool) -> None:
        self._speaking = speaking
        if not speaking and self.jarvis_mode:
            self.listener.reset()
        self._refresh_state()

    def _on_voice_error(self, message: str, hint: str = "") -> None:
        self.ui.post("error", message, hint)
