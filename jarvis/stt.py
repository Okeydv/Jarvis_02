"""Распознавание речи (Vosk): одна команда по кнопке/горячей клавише или режим «Джарвис»."""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"


class VoiceError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


class Listener:
    """Слушает микрофон в отдельном потоке.

    on_partial(text) — промежуточный результат; on_final(text, mode) — фраза целиком,
    mode = "command" (ждали команду) или "wake" (режим «Джарвис», нужна проверка
    слова-активатора); on_listening(bool) — идёт ли ожидание команды.
    """

    def __init__(self, config, on_partial: Callable[[str], None], on_final: Callable[[str, str], None],
                 on_listening: Callable[[bool], None], on_error: Callable[[str, str], None]):
        self.config = config
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_listening = on_listening
        self.on_error = on_error
        self.available = False
        self._model = None
        self._recognizer = None
        self._stream = None
        self._audio: queue.Queue[bytes] = queue.Queue(maxsize=300)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._continuous = False
        self._command = False
        self._deadline = 0.0
        self._speech_started = 0.0
        self._reset = False
        self._last_partial = ""

    # ─── загрузка ───
    def load(self) -> None:
        path = self.config.resolve_path(self.config.get("voice.vosk_model_path"))
        if path is None or not path.is_dir():
            raise VoiceError(
                f"Модель распознавания речи Vosk не найдена: {path}.",
                f"Запустите «python download_models.py» или скачайте {VOSK_URL}, распакуйте в папку models "
                "и проверьте voice.vosk_model_path в config.yaml. Пока работает текстовый режим.",
            )
        try:
            import sounddevice  # noqa: F401
            import vosk
        except ImportError as exc:
            raise VoiceError(f"Не установлена библиотека для голоса: {exc.name}.",
                             "Выполните: pip install -r requirements.txt") from None
        except OSError as exc:  # нет PortAudio
            raise VoiceError(f"Не удалось загрузить аудиобиблиотеку: {exc}") from None
        vosk.SetLogLevel(-1)
        try:
            self._model = vosk.Model(str(path))
        except Exception as exc:
            raise VoiceError(f"Не удалось загрузить модель Vosk из {path}: {exc}",
                             "Скачайте модель заново: python download_models.py --vosk") from None
        self._input_device()  # проверка микрофона
        self.available = True
        self._thread = threading.Thread(target=self._run, name="stt", daemon=True)
        self._thread.start()

    def _resolve_device(self):
        import sounddevice as sd

        device = self.config.get("voice.input_device")
        if device is None or device == "":
            return None
        if isinstance(device, int) or str(device).isdigit():
            return int(device)
        wanted = str(device).casefold()
        for index, info in enumerate(sd.query_devices()):
            if info.get("max_input_channels", 0) > 0 and wanted in info.get("name", "").casefold():
                return index
        raise VoiceError(f"Микрофон «{device}» не найден.", "Проверьте voice.input_device в config.yaml (null — по умолчанию).")

    def _input_device(self):
        import sounddevice as sd

        try:
            device = self._resolve_device()
            info = sd.query_devices(device, "input")
        except VoiceError:
            raise
        except Exception as exc:
            raise VoiceError(f"Микрофон не найден ({exc}).",
                             "Подключите микрофон и проверьте, что он включён в «Параметры → Система → Звук». "
                             "Текстовый режим работает.") from None
        return device, info

    # ─── управление ───
    def set_continuous(self, enabled: bool) -> None:
        with self._lock:
            self._continuous = enabled
            self._reset = True
        self._wake.set()

    def listen_command(self, timeout: float | None = None) -> None:
        """Следующая фраза — команда (без слова-активатора)."""
        if not self.available:
            return
        wait = float(timeout if timeout is not None else self.config.get("voice.listen_timeout", 7))
        with self._lock:
            self._command = True
            self._deadline = time.monotonic() + wait
            self._speech_started = 0.0
            self._reset = True
        self._wake.set()
        self.on_listening(True)

    def cancel_command(self) -> None:
        with self._lock:
            was_active, self._command = self._command, False
        if was_active:
            self.on_listening(False)

    @property
    def command_active(self) -> bool:
        return self._command

    def reset(self) -> None:
        """Сбросить накопленный звук (например, после того как Джарвис договорил)."""
        self._reset = True

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._close_stream()

    # ─── поток распознавания ───
    def _callback(self, indata, frames, time_info, status) -> None:
        try:
            self._audio.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def _open_stream(self) -> None:
        import sounddevice as sd
        import vosk

        device, info = self._input_device()
        rate = int(info.get("default_samplerate") or 16000)
        try:
            self._recognizer = vosk.KaldiRecognizer(self._model, rate)
            self._stream = sd.RawInputStream(samplerate=rate, blocksize=int(rate * 0.1), device=device,
                                             dtype="int16", channels=1, callback=self._callback)
            self._stream.start()
        except Exception as exc:
            self._stream = None
            raise VoiceError(f"Не удалось открыть микрофон: {exc}",
                             "Проверьте, не занят ли микрофон другой программой, и разрешён ли доступ к нему "
                             "в «Параметры → Конфиденциальность → Микрофон».") from None
        log.info("Микрофон открыт: %s, %d Гц", info.get("name"), rate)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._drain()

    def _drain(self) -> None:
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return

    def _publish_partial(self, text: str) -> None:
        if text != self._last_partial:
            self._last_partial = text
            self.on_partial(text)

    def _deliver(self, text: str) -> None:
        with self._lock:
            command, self._command = self._command, False
            continuous = self._continuous
        if command:
            self.on_listening(False)
            self.on_final(text, "command")
        elif continuous:
            self.on_final(text, "wake")

    def _check_timeouts(self, now: float) -> None:
        max_phrase = float(self.config.get("voice.max_phrase_seconds", 15))
        timed_out = forced = False
        with self._lock:
            if not self._command:
                return
            if not self._speech_started and now > self._deadline:
                self._command = False
                timed_out = True
            elif self._speech_started and now - self._speech_started > max_phrase:
                forced = True
        if timed_out:
            self._publish_partial("")
            self.on_listening(False)
        elif forced and self._recognizer is not None:
            text = json.loads(self._recognizer.FinalResult()).get("text", "").strip()
            self._publish_partial("")
            if text:
                self._deliver(text)
            else:
                self.cancel_command()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                needed = self._continuous or self._command
            if not needed:
                if self._stream is not None:
                    self._close_stream()
                    self._publish_partial("")
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if self._stream is None:
                try:
                    self._open_stream()
                except VoiceError as exc:
                    with self._lock:
                        self._continuous = self._command = False
                    self.on_listening(False)
                    self.on_error(exc.message, exc.hint)
                    continue
            try:
                data = self._audio.get(timeout=0.1)
            except queue.Empty:
                data = None
            if self._reset:
                self._reset = False
                self._drain()
                if self._recognizer is not None:
                    self._recognizer.Reset()
                self._publish_partial("")
                continue
            now = time.monotonic()
            if data is not None and self._recognizer is not None:
                try:
                    if self._recognizer.AcceptWaveform(data):
                        text = json.loads(self._recognizer.Result()).get("text", "").strip()
                        self._publish_partial("")
                        if text:
                            self._deliver(text)
                    else:
                        partial = json.loads(self._recognizer.PartialResult()).get("partial", "").strip()
                        if partial and not self._speech_started:
                            self._speech_started = now
                        self._publish_partial(partial)
                except Exception as exc:
                    log.exception("Ошибка распознавания")
                    self.on_error(f"Ошибка распознавания речи: {exc}", "")
                    self._reset = True
            self._check_timeouts(now)
        self._close_stream()
