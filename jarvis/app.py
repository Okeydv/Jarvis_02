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

import psutil

from . import winapi
from .agent import Agent
from .config import Config
from .hotkey import GlobalHotkey
from .llm import BACKEND_TITLES, LLMBackend, LLMError, create_backend
from .llm.base import ToolCall
from .memory import HistoryStore
from .proactive import Proactive, Suggestion, suggestion_answer
from .routines import Scheduler
from .storage import data_folder
from .stt import ECHO_TAIL_SECONDS, Listener, VoiceError, list_input_devices
from .text_utils import SentenceSplitter, contains_stop_word, find_wake_word, is_stop_phrase, normalize
from .tools import Confirmation, ToolRegistry, ToolResult, ToolServices
from .tts import Speaker, SpeechError
from .ui import MainWindow
from .vision import VisionService

log = logging.getLogger(__name__)

# Какой бэкенд использует ключ из .env
_SECRET_BACKENDS = {"GIGACHAT_CREDENTIALS": "gigachat", "GEMINI_API_KEY": "gemini", "BAZAARLINK_API_KEY": "qwen"}
# Ответы на предложение Джарвиса голосом или текстом
SUGGESTION_ANSWER_SECONDS = 90


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
        self.jarvis_mode = bool(config.get("voice.jarvis_mode", True))
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
        self._wake_until = 0.0
        self._speaking = False
        self._closing = False
        self._suggestions: dict[str, Suggestion] = {}
        self._pending_suggestion: tuple[Suggestion, float] | None = None
        self._listen_after_speech = False
        self._log_ids = itertools.count(1)

        self.speaker = Speaker(config, on_speaking=self._on_speaking, on_error=self._on_voice_error)
        self.listener = Listener(config, on_partial=self._on_partial, on_final=self._on_final,
                                 on_listening=self._on_listening, on_error=self._on_voice_error,
                                 on_wake=self._on_wake, on_device=self._on_device)
        self.services = ToolServices(config, confirm=self.ask_confirmation, notify=self.notify, focus=self.focus)
        self.services.on_step = self._on_nested_step
        self.services.vision = VisionService(config, self._get_backend, lambda: self.backend_name)
        self.registry = ToolRegistry(self.services)
        self.agent = Agent(config, self.registry, self._backend, self, context=self._context_text)
        folder = data_folder(config)
        self.history_store = HistoryStore(folder / "history.json")
        self.scheduler = Scheduler(self.services.schedule, self._on_schedule_due)
        self.proactive = Proactive(config, folder / "proactive.json", deliver=self._deliver_suggestion,
                                   on_briefing=self._auto_briefing, on_return=self._on_user_return,
                                   idle_seconds=winapi.idle_seconds,
                                   busy=lambda: self._current is not None or self._speaking
                                   or self._pending_confirm is not None)
        self.hotkey = GlobalHotkey(str(config.get("hotkey", "ctrl+alt+j")), self._on_hotkey)
        self.ui = MainWindow(self)

    # ═══ запуск и остановка ═══
    def run(self) -> None:
        self.ui.post("settings", self.jarvis_mode, self.speak_enabled)
        self.ui.post("backend", self.backend_name, self._model_name(self.backend_name))
        self.ui.post("voice_available", False)
        for error in self.config.load_errors:
            self.ui.post("error", error, "")
        self.ui.post("dnd", bool(self.config.get("proactive.dnd", False)))
        self._restore_history()
        self._refresh_state()
        threading.Thread(target=self._agent_worker, name="agent", daemon=True).start()
        threading.Thread(target=self._load_voice, name="loader", daemon=True).start()
        threading.Thread(target=self._check_backend, args=(self.backend_name,), name="check", daemon=True).start()
        if not self.hotkey.start():
            self.ui.post("error", f"Глобальная горячая клавиша не работает: {self.hotkey.error}", "")
        self.scheduler.start()
        self.proactive.start()
        self.ui.mainloop()

    def _restore_history(self) -> None:
        """Разговор продолжается после перезапуска (memory.save_history)."""
        if not self.config.get("memory.save_history", True):
            return
        history = self.history_store.load()
        if not history:
            return
        self.agent.history = history
        shown = [(m["role"], m["content"]) for m in history
                 if m["role"] in ("user", "assistant") and (m.get("content") or "").strip()]
        self.ui.post("history", shown[-16:])

    def shutdown(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.stop()
        self._requests.put(None)
        self.hotkey.stop()
        self.scheduler.stop()
        self.proactive.stop()
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
        if self._listening or time.monotonic() < self._wake_until:
            return "listening"
        if self._loading:
            return "loading"
        return "idle"

    def _refresh_state(self) -> None:
        state = self._compute_state()
        subtitle = ""
        if state == "loading":
            subtitle = self._loading_text
        elif state == "listening":
            subtitle = "говорите команду"
        elif state == "idle":
            if self.jarvis_mode and self.listener.available:
                subtitle = "скажите «Джарвис, …»"
            elif self.listener.available:
                subtitle = f"нажмите «Микрофон» или {str(self.config.get('hotkey', 'ctrl+alt+j')).upper()}"
        self.ui.post("state", state, subtitle)

    def _set_loading(self, text: str) -> None:
        self._loading_text = text
        self._refresh_state()

    # ═══ загрузка моделей ═══
    def _load_voice(self) -> None:
        voice_ok = False
        self._set_loading("распознавание речи")
        try:
            self.listener.load(status=self._set_loading)
            voice_ok = True
        except VoiceError as exc:
            self.ui.post("error", f"Голосовой ввод не работает: {exc.message}", exc.hint)
        except Exception as exc:
            log.exception("Сбой загрузки Vosk")
            self.ui.post("error", f"Не удалось запустить распознавание речи: {exc}", "")
        if not voice_ok:
            self.jarvis_mode = False  # без микрофона режим недоступен (настройку не сохраняем)
        self.ui.post("voice_available", voice_ok)
        self.ui.post("settings", self.jarvis_mode, self.speak_enabled)
        if voice_ok and self.jarvis_mode:
            self.listener.set_continuous(True)

        self._set_loading("синтез речи")
        try:
            self.speaker.load(status=self._set_loading)
        except SpeechError as exc:
            self.ui.post("error", f"Озвучка ответов не работает: {exc.message}", exc.hint)
        except Exception as exc:
            log.exception("Сбой загрузки Silero")
            self.ui.post("error", f"Не удалось запустить синтез речи: {exc}", "")

        self._loading = False
        combo = str(self.config.get("hotkey", "ctrl+alt+j")).upper()
        if voice_ok and self.jarvis_mode:
            self.ui.post("system", f"Джарвис готов и слушает. Скажите «Джарвис, открой браузер» — или нажмите {combo}, "
                                   "или напишите команду.")
        elif voice_ok:
            self.ui.post("system", f"Джарвис готов. Нажмите «Микрофон» или {combo} и говорите, либо напишите команду.")
        else:
            self.ui.post("system", "Джарвис готов к текстовым командам.")
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
            note = backend.check()
            if name == self.backend_name:
                self.ui.post("system", f"{BACKEND_TITLES[name]}: модель {backend.model} на связи. "
                                       f"Инструментов управления ПК: {len(self.registry.schemas())}.")
                if note:
                    self.ui.post("system", note)
            schemas = self.registry.schemas()
            backend.warmup(self.agent.build_system(schemas), schemas)  # тот же промпт, что в запросах — для кэша
        except LLMError as exc:
            if name == self.backend_name:
                self.ui.post("error", exc.message, exc.hint)
        except Exception as exc:
            log.exception("Проверка бэкенда %s", name)
            if name == self.backend_name:
                self.ui.post("error", f"{BACKEND_TITLES.get(name, name)}: {type(exc).__name__}: {exc}", "")

    def _drop_backend(self, name: str) -> None:
        with self._backend_lock:
            backend = self._backends.pop(name, None)
        if backend is not None:
            backend.close()

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
        if self._answer_by_words(text):
            return
        self.stop(keep_listening=True)  # новый запрос прерывает текущий
        self._enqueue(text, source)

    def _enqueue(self, text: str, source: str, show: str | None = None) -> None:
        """Запрос в очередь агента (без прерывания текущего — для расписания и предложений)."""
        request_id = next(self._request_ids)
        cancel = threading.Event()
        self.ui.post("user", request_id, show or text, source)
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
            self._wake_until = 0.0
        self._refresh_state()

    def toggle_mic(self) -> None:
        if not self.listener.available:
            self.ui.post("error", "Голосовой ввод недоступен.",
                         "Нужны модель Vosk и микрофон — подробности в сообщении при запуске и в «Настройки → "
                         "Диагностика». Текстовый режим работает.")
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
            self.ui.post("error", "Режим «Джарвис» недоступен без микрофона и модели распознавания речи.", "")
            return
        self.jarvis_mode = enabled
        self.config.set("voice.jarvis_mode", enabled, persist=True)
        self.listener.set_continuous(enabled)
        self.ui.post("system", "Режим «Джарвис» включён: начинайте команду со слова «Джарвис»." if enabled
                     else "Режим «Джарвис» выключен: для голосовой команды нажмите «Микрофон».")
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
        self.ui.post("system", f"Модель: {BACKEND_TITLES.get(name, name)} ({self._model_name(name)}).")
        threading.Thread(target=self._check_backend, args=(name,), name="check", daemon=True).start()

    def new_dialog(self) -> None:
        self.stop()
        self.agent.reset()
        self.history_store.clear()
        self.ui.post("clear")
        self.ui.post("system", "Новый диалог: история очищена. То, что я запомнил о вас, осталось — «Настройки → Память».")

    # ─── настройки ───
    def list_microphones(self) -> list[dict]:
        return list_input_devices()

    def apply_settings(self, changes: dict) -> None:
        """Применяет и сохраняет настройки из окна «Настройки»."""
        for key, value in changes.items():
            self.config.set(key, value, persist=True)
        if {"voice.input_device"} & changes.keys():
            self.listener.restart()
        if "tts.speaker" in changes:
            self.speaker.set_voice(str(changes["tts.speaker"]))
        for backend in BACKEND_TITLES:
            if any(key.startswith(backend + ".") for key in changes):
                self._reset_backend(backend)
        if "tools.allow_powershell" in changes:
            self.ui.post("system", "Команды PowerShell разрешены — каждую нужно будет подтвердить."
                         if changes["tools.allow_powershell"] else "Команды PowerShell запрещены.")
        if "ui.hud" in changes:
            self.ui.post("call", self.ui.set_hud, bool(changes["ui.hud"]))
        if "tools.allow_code" in changes:
            self.ui.post("system", "Запуск кода Python разрешён — каждый запуск нужно будет подтвердить."
                         if changes["tools.allow_code"] else "Запуск кода запрещён (писать код в файлы Джарвис может).")
        log.info("Настройки изменены: %s", ", ".join(changes))

    def save_secrets(self, secrets: dict[str, str]) -> None:
        """Ключи API из окна «Настройки» → файл .env; бэкенд пересоздаётся с новым ключом."""
        for name, value in secrets.items():
            try:
                self.config.save_secret(name, value)
            except Exception as exc:
                log.exception("Не удалось сохранить ключ %s", name)
                self.ui.post("error", f"Не удалось сохранить ключ {name} в файл .env: {exc}", "")
                return
        for name in secrets:
            backend = _SECRET_BACKENDS.get(name)
            if not backend:
                continue
            title = BACKEND_TITLES.get(backend, backend)
            if backend == self.backend_name:
                self.ui.post("system", f"Ключ {title} сохранён — проверяю связь с моделью…")
            else:
                self.ui.post("system", f"Ключ {title} сохранён. Чтобы им пользоваться, выберите «{title}» "
                                       "вверху окна.")
            self._reset_backend(backend)
        log.info("Сохранены ключи: %s", ", ".join(secrets))

    def _reset_backend(self, name: str) -> None:
        self._drop_backend(name)
        if name == self.backend_name:
            self.ui.post("backend", name, self._model_name(name))
            threading.Thread(target=self._check_backend, args=(name,), name="check", daemon=True).start()

    def preview_voice(self, name: str) -> None:
        if not self.speaker.available:
            self.ui.post("error", "Синтез речи ещё не загружен.", "")
            return
        self.speaker.stop()
        self.speaker.set_voice(name)
        self.speaker.say("Добрый день, сэр. Так звучит мой голос.")

    def self_test_tools(self) -> list[str]:
        from .diagnostics import tools_self_test

        return tools_self_test(self.registry)

    def run_diagnostics(self, emit) -> str:
        from .diagnostics import run_diagnostics

        extra = [("✓ Горячая клавиша " + str(self.config.get("hotkey", "")).upper() + " работает") if not self.hotkey.error
                 else f"✗ Горячая клавиша: {self.hotkey.error}"]
        backend = None
        try:
            backend = self._backend()
        except LLMError:
            pass
        return run_diagnostics(self.config, emit, listener=self.listener, speaker=self.speaker,
                               registry=self.registry, backend=backend, play=False,
                               prompt=lambda message: emit(">>> " + message), extra=extra)

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
                if self.config.get("memory.save_history", True):
                    self.history_store.save(self.agent.history)

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
        self._log_start(call.id, call.name, call.arguments)

    def on_tool_result(self, call: ToolCall, result: ToolResult) -> None:
        self._log_result(call.id, result)

    def _log_start(self, log_id: str, name: str, arguments: dict) -> None:
        text = json.dumps(arguments, ensure_ascii=False)
        if len(text) > 300:
            text = text[:300] + "…"
        self.ui.post("log_start", log_id, datetime.now().strftime("%H:%M:%S"), name, text)

    def _log_result(self, log_id: str, result: ToolResult) -> None:
        text = result.text if len(result.text) <= 600 else result.text[:600] + "…"
        self.ui.post("log_result", log_id, result.status, text)

    def _on_nested_step(self, name: str, arguments: dict, result: ToolResult) -> None:
        """Шаг сценария, запущенного моделью, — отдельной карточкой в журнале."""
        log_id = f"step-{next(self._log_ids)}"
        self._log_start(log_id, name, arguments)
        self._log_result(log_id, result)

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
        self.proactive.note_event(text)
        self._speak(text)

    # ═══ контекст, расписание, проактивность ═══
    def _context_text(self) -> str:
        """Активное окно пользователя — чтобы понимать «исправь это», «что тут»."""
        if not winapi.IS_WINDOWS or not self.config.get("context.active_window", True):
            return ""
        hwnd = self.focus.target()
        title = winapi.window_title(hwnd).strip() if hwnd else ""
        if not title:
            return ""
        try:
            process = psutil.Process(winapi.window_pid(hwnd)).name()
        except psutil.Error:
            process = ""
        return f"Активное окно пользователя: «{title[:120]}»" + (f" ({process})" if process else "")

    def _on_schedule_due(self, task: dict) -> None:  # поток scheduler
        what = f"сценарий «{task['routine']}»" if task.get("routine") else f"«{task.get('prompt')}»"
        self.ui.post("system", f"⏰ По расписанию «{task['name']}»: {what}.")
        self.proactive.note_event(f"по расписанию «{task['name']}»")
        if task.get("routine"):
            self._enqueue(f"запусти сценарий {task['routine']}", "schedule")
        else:
            self._enqueue(task["prompt"], "schedule")

    def _quiet_now(self) -> bool:
        return self.proactive.quiet(datetime.now())

    def _deliver_suggestion(self, suggestion: Suggestion) -> None:  # поток proactive
        self._suggestions[suggestion.id] = suggestion
        self._pending_suggestion = (suggestion, time.monotonic())
        self.ui.post("suggestion", suggestion)
        if self.speak_enabled and self.speaker.available and not self._quiet_now():
            self.speaker.earcon("start")
            self.speaker.say(suggestion.text)
            # Ответить можно голосом («да» / «нет») — сразу после вопроса Джарвис слушает.
            self._listen_after_speech = self.listener.available

    def answer_suggestion(self, suggestion_id: str, answer: str) -> None:
        """answer: yes / later / never — кнопки карточки или голос."""
        suggestion = self._suggestions.pop(suggestion_id, None)
        if suggestion is None:
            return
        if self._pending_suggestion and self._pending_suggestion[0].id == suggestion_id:
            self._pending_suggestion = None
        self.proactive.feedback(suggestion.kind, answer)
        self.ui.post("suggestion_answered", suggestion_id, answer)
        if answer == "yes":
            self._enqueue(suggestion.prompt, "suggestion", show="Да, давай")
        elif answer == "never":
            self.ui.post("system", "Хорошо, сэр, такое больше не предлагаю (вернуть — «Настройки → Память → "
                                   "Снова предлагать всё»).")

    def _answer_by_words(self, text: str) -> bool:
        """«Да» / «нет» сразу после предложения Джарвиса — ответ на него."""
        pending = self._pending_suggestion
        if pending is None or time.monotonic() - pending[1] > SUGGESTION_ANSWER_SECONDS:
            return False
        words = normalize(text)
        found, rest = find_wake_word(words, self.config.get("voice.wake_words", ["джарвис"]))
        words = rest if found else words
        answer = suggestion_answer(words)
        if answer:
            self.answer_suggestion(pending[0].id, answer)
            return True
        self._pending_suggestion = None  # ответил чем-то другим — это новый запрос
        return False

    def _auto_briefing(self) -> None:  # поток proactive
        from .briefing import compose

        text = compose(self.services)
        self.ui.post("jarvis_note", "Утренний брифинг", text)
        if self.speak_enabled and self.speaker.available and not self._quiet_now():
            self.speaker.say(text)

    def briefing_now(self) -> None:
        threading.Thread(target=self._auto_briefing, name="briefing", daemon=True).start()

    def _on_user_return(self, summary: str) -> None:  # поток proactive
        self.ui.post("jarvis_note", "С возвращением", summary)
        if self.speak_enabled and self.speaker.available and not self._quiet_now():
            self.speaker.say("С возвращением, сэр. Пока вас не было, кое-что произошло — подробности в окне.")

    def set_dnd(self, enabled: bool) -> None:
        self.config.set("proactive.dnd", enabled, persist=True)
        self.ui.post("system", "Режим «Не беспокоить»: предложений не будет." if enabled
                     else "Режим «Не беспокоить» выключен: буду предлагать помощь, когда замечу что-то важное.")

    # ═══ голос ═══
    def _on_hotkey(self) -> None:  # поток hotkey
        self.toggle_mic()

    def _on_device(self, name: str) -> None:
        self.ui.post("mic_device", name)

    def _on_listening(self, active: bool) -> None:
        self._listening = active
        self.ui.post("mic", active)
        if not active:
            self.ui.post("heard", "")
        self._refresh_state()

    def _on_wake(self) -> None:
        """«Джарвис» прозвучал в начале фразы — сразу показываем, что слушаем команду."""
        if self._speaking or self.speaker.busy:
            return
        self._wake_until = time.monotonic() + float(self.config.get("voice.max_phrase_seconds", 15))
        self._refresh_state()

    def _on_partial(self, text: str) -> None:
        self.ui.post("heard", text)
        busy = self._current is not None or self._speaking or self.speaker.busy
        if (text and busy and contains_stop_word(text, self.config.get("voice.stop_words", []))
                and not self.speaker.is_echo(text)):
            self.stop()
            self.listener.reset()

    def _on_final(self, text: str, mode: str, wake_heard: bool = False) -> None:
        self.ui.post("heard", "")
        self._wake_until = 0.0
        if mode == "command":
            self.submit_text(text, "voice")
            self._refresh_state()
            return
        # Режим «Джарвис». Микрофон слышит и голос самого Джарвиса, поэтому во время речи
        # фразы, похожие на только что сказанное, отбрасываются как эхо.
        speaking = self._speaking or self.speaker.busy
        if speaking and self.speaker.is_echo(text):
            self._refresh_state()
            return
        wake_words = self.config.get("voice.wake_words", ["джарвис"])
        found, command = find_wake_word(text, wake_words)
        if not found and wake_heard and not speaking:
            # «Джарвис» был в промежуточном результате, но в итоговом распознался иначе:
            # считаем первое слово обращением.
            words = normalize(text).split()
            found, command = bool(words), " ".join(words[1:])
        if not found:
            if speaking and contains_stop_word(text, self.config.get("voice.stop_words", [])):
                self.stop()
            self._refresh_state()
            return
        if command:
            self.submit_text(command, "voice")  # прерывает текущий ответ
        else:
            self.stop(keep_listening=True)
            self.listener.listen_command(float(self.config.get("voice.follow_up_seconds", 6)))
            self.speaker.earcon("start")
        self._refresh_state()

    def _on_speaking(self, speaking: bool) -> None:
        self._speaking = speaking
        if not speaking and self.jarvis_mode:
            self.listener.reset(hold=ECHO_TAIL_SECONDS)
        if not speaking and self._listen_after_speech:
            self._listen_after_speech = False
            self.listener.listen_command(float(self.config.get("voice.follow_up_seconds", 6)), hold=ECHO_TAIL_SECONDS)
        self._refresh_state()

    def _on_voice_error(self, message: str, hint: str = "") -> None:
        self.ui.post("error", message, hint)
