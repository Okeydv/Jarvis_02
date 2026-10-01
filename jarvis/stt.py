"""Распознавание речи (Vosk): команда по кнопке/горячей клавише и режим «Джарвис».

Модуль сам выбирает рабочий микрофон, показывает уровень сигнала, замечает «полную
тишину» (так выглядит запрет доступа к микрофону в Windows) и узнаёт слово «Джарвис»
уже по промежуточным результатам распознавания.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import threading
import time
from typing import Callable

import numpy as np

from . import winapi
from .downloads import ensure_vosk_model, vosk_model_ready
from .text_utils import find_wake_word

log = logging.getLogger(__name__)

PRIVACY_HINT = (
    "Windows не даёт программе доступ к микрофону или он выключен. Откройте «Параметры → "
    "Конфиденциальность и защита → Микрофон» и включите «Доступ к микрофону» и «Разрешить "
    "классическим приложениям доступ к микрофону». Проверьте, что микрофон не отключён кнопкой "
    "и что в настройках Джарвиса выбран нужный микрофон."
)
SILENCE_SECONDS = 8.0  # столько секунд абсолютных нулей — и сообщаем, что микрофон, видимо, заблокирован
_MAPPER_NAMES = ("sound mapper", "переназначение звуковых", "primary sound capture", "первичный драйвер записи")


class VoiceError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


def _load_vosk_model(vosk, path):
    """Vosk (Kaldi) в Windows не открывает пути с кириллицей, например C:\\Users\\Иван\\…
    Тогда берём короткий путь 8.3, а если его нет — грузим модель по относительному
    пути из её родительской папки."""
    text = winapi.short_path(str(path))
    if text.isascii() or not winapi.IS_WINDOWS or not path.name.isascii():
        return vosk.Model(text)
    previous = os.getcwd()
    os.chdir(path.parent)
    try:
        return vosk.Model(path.name)
    finally:
        os.chdir(previous)


def list_input_devices() -> list[dict]:
    """Микрофоны для выбора в настройках: по одному на название (из основного звукового API)."""
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        default_api = sd.default.hostapi
        default_input = sd.default.device[0]
    except Exception as exc:
        log.warning("Не удалось получить список микрофонов: %s", exc)
        return []
    result, seen = [], set()
    for index, info in enumerate(devices):
        name = str(info.get("name", "")).strip()
        if info.get("max_input_channels", 0) <= 0 or info.get("hostapi") != default_api:
            continue
        if not name or name in seen or any(m in name.lower() for m in _MAPPER_NAMES):
            continue
        seen.add(name)
        result.append({"index": index, "name": name, "default": index == default_input})
    return result


def level_from_rms(rms: float) -> float:
    """Громкость 0…1 по шкале децибел (−60 дБ — тишина, −10 дБ — громко)."""
    if rms <= 0:
        return 0.0
    db = 20 * math.log10(rms)
    return min(1.0, max(0.0, (db + 60) / 50))


class Listener:
    """Слушает микрофон в отдельном потоке.

    on_partial(text) — промежуточный результат; on_final(text, mode, wake_heard) — фраза
    целиком, mode = "command" (ждали команду) или "wake" (режим «Джарвис»); wake_heard —
    «Джарвис» прозвучал в начале фразы (по промежуточным результатам); on_wake() — слово
    «Джарвис» только что услышано; on_listening(bool) — идёт ли ожидание команды;
    on_device(name) — какой микрофон открыт.
    """

    def __init__(self, config, on_partial: Callable[[str], None], on_final: Callable[..., None],
                 on_listening: Callable[[bool], None], on_error: Callable[[str, str], None],
                 on_wake: Callable[[], None] | None = None, on_device: Callable[[str], None] | None = None):
        self.config = config
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_listening = on_listening
        self.on_error = on_error
        self.on_wake = on_wake or (lambda: None)
        self.on_device = on_device or (lambda name: None)
        self.available = False
        self.device_name = ""
        self.level = 0.0
        self._model = None
        self._vosk = None
        self._recognizer = None
        self._stream = None
        self._rate = 16000
        self._audio: queue.Queue[bytes] = queue.Queue(maxsize=300)
        self._taps: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._continuous = False
        self._command = False
        self._probing = 0
        self._deadline = 0.0
        self._speech_started = 0.0
        self._command_peak = 0.0
        self._reset = False
        self._restart = False
        self._last_partial = ""
        self._wake_heard = False
        self._opened_at = 0.0
        self._peak_since_open = 0
        self._silence_reported = False
        self._quiet_reported = False
        self._missing_reported = None

    # ─── загрузка ───
    def load(self, status: Callable[[str], None] = lambda text: None) -> None:
        path = self.config.resolve_path(self.config.get("voice.vosk_model_path"))
        try:
            import sounddevice  # noqa: F401
            import vosk
        except ImportError as exc:
            raise VoiceError(f"Не установлена библиотека для голоса: {exc.name}.",
                             "Выполните: pip install -r requirements.txt") from None
        except OSError as exc:  # нет PortAudio
            raise VoiceError(f"Не удалось загрузить аудиобиблиотеку: {exc}") from None
        if not vosk_model_ready(path):
            url = str(self.config.get("voice.vosk_model_url"))
            status("скачиваю модель распознавания речи (≈45 МБ)")
            try:
                ensure_vosk_model(url, path, lambda p: status(f"скачиваю модель распознавания речи: {p}%"))
            except Exception as exc:
                raise VoiceError(
                    f"Не удалось скачать модель распознавания речи: {exc}",
                    f"Скачайте {url}, распакуйте в папку models и проверьте voice.vosk_model_path "
                    "в config.yaml. Пока работает текстовый режим.",
                ) from None
        status("загружаю распознавание речи")
        vosk.SetLogLevel(-1)
        try:
            self._model = _load_vosk_model(vosk, path)
        except Exception as exc:
            hint = "Удалите папку модели — при следующем запуске она скачается заново."
            if not str(path).isascii():
                hint = ("Vosk не понимает русские буквы в пути к модели. Перенесите папку с Джарвисом в путь "
                        "без кириллицы (например C:\\Jarvis).")
            raise VoiceError(f"Не удалось загрузить модель Vosk из {path}: {exc}", hint) from None
        self._vosk = vosk
        self._check_any_microphone()
        self.available = True
        self._thread = threading.Thread(target=self._run, name="stt", daemon=True)
        self._thread.start()

    def _check_any_microphone(self) -> None:
        import sounddevice as sd

        try:
            inputs = [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
        except Exception as exc:
            raise VoiceError(f"Не удалось получить список аудиоустройств: {exc}") from None
        if not inputs:
            raise VoiceError("Микрофон не найден.",
                             "Подключите микрофон и проверьте, что он включён в «Параметры → Система → Звук». "
                             "Текстовый режим работает.")
        log.info("Микрофоны: %s", ", ".join(sorted({d['name'] for d in inputs})))

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
            self._command_peak = 0.0
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

    @property
    def stream_open(self) -> bool:
        return self._stream is not None

    def reset(self) -> None:
        """Сбросить накопленный звук (например, после того как Джарвис договорил)."""
        self._reset = True

    def restart(self) -> None:
        """Переоткрыть микрофон (сменили устройство в настройках)."""
        self._restart = True
        self._silence_reported = False
        self._wake.set()

    def probe(self, seconds: float = 4.0, on_level: Callable[[float], None] | None = None) -> dict:
        """Проверка микрофона: записывает несколько секунд и распознаёт сказанное."""
        if not self.available:
            raise VoiceError("Распознавание речи не загружено.")
        tap: queue.Queue = queue.Queue()
        with self._lock:
            self._taps.append(tap)
            self._probing += 1
        self._wake.set()
        chunks: list[bytes] = []
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                try:
                    chunks.append(tap.get(timeout=0.1))
                except queue.Empty:
                    pass
                if on_level:
                    on_level(self.level)
        finally:
            with self._lock:
                self._taps.remove(tap)
                self._probing -= 1
            self._reset = True  # хвост проверочной фразы не должен распознаться как команда
        audio = b"".join(chunks)
        samples = np.frombuffer(audio, dtype=np.int16)
        peak = int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0
        rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768) ** 2))) if samples.size else 0.0
        text = ""
        if samples.size and self._vosk is not None:
            recognizer = self._vosk.KaldiRecognizer(self._model, self._rate)
            recognizer.AcceptWaveform(audio)
            text = json.loads(recognizer.FinalResult()).get("text", "")
        return {"device": self.device_name, "rate": self._rate, "seconds": len(samples) / max(1, self._rate),
                "peak": peak / 32768, "rms_db": 20 * math.log10(rms) if rms > 0 else -120.0,
                "silent": peak == 0 and samples.size > 0, "no_audio": samples.size == 0, "text": text}

    def shutdown(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._close_stream()

    # ─── микрофон ───
    def _callback(self, indata, frames, time_info, status) -> None:
        try:
            self._audio.put_nowait(bytes(indata))
        except queue.Full:
            pass

    def _candidates(self) -> list:
        """Порядок попыток: выбранный микрофон (в разных звуковых API) → микрофон по умолчанию."""
        import sounddevice as sd

        wanted = self.config.get("voice.input_device")
        devices = list(enumerate(sd.query_devices()))
        inputs = [(i, d) for i, d in devices if d.get("max_input_channels", 0) > 0]
        order: list = []
        if wanted not in (None, "", "default"):
            if isinstance(wanted, int) or str(wanted).isdigit():
                order.append(int(wanted))
            else:
                name = str(wanted).casefold()
                default_api = sd.default.hostapi
                exact = [i for i, d in inputs if d["name"].casefold() == name]
                partial = [i for i, d in inputs if name in d["name"].casefold() and i not in exact]
                order += sorted(exact + partial, key=lambda i: (devices[i][1]["hostapi"] != default_api, i))
                if not order and self._missing_reported != wanted:
                    self._missing_reported = wanted
                    log.warning("Микрофон «%s» не найден — использую микрофон по умолчанию", wanted)
                    self.on_error(f"Микрофон «{wanted}» не найден — использую микрофон по умолчанию.",
                                  "Выберите микрофон в «Настройки → Голос».")
        order.append(None)  # системный микрофон по умолчанию
        result = []
        for item in order:
            if item not in result:
                result.append(item)
        return result

    def _open_stream(self) -> None:
        import sounddevice as sd

        errors = []
        for device in self._candidates():
            try:
                info = sd.query_devices(device, "input")
                rate = int(info.get("default_samplerate") or 16000)
                stream = sd.RawInputStream(samplerate=rate, blocksize=int(rate * 0.1), device=device,
                                           dtype="int16", channels=1, callback=self._callback)
                stream.start()
            except Exception as exc:
                errors.append(f"{device if device is not None else 'по умолчанию'}: {exc}")
                log.warning("Не удалось открыть микрофон %s: %s", device, exc)
                continue
            self._rate = rate
            self._recognizer = self._vosk.KaldiRecognizer(self._model, rate)
            self._stream = stream
            self.device_name = str(info.get("name", ""))
            self._opened_at = time.monotonic()
            self._peak_since_open = 0
            self._drain()
            log.info("Микрофон открыт: %s, %d Гц", self.device_name, rate)
            self.on_device(self.device_name)
            return
        raise VoiceError(f"Не удалось открыть микрофон ({'; '.join(errors)}).",
                         "Проверьте, что микрофон подключён и не занят другой программой. " + PRIVACY_HINT)

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self.level = 0.0
        self._drain()

    def _drain(self) -> None:
        while True:
            try:
                self._audio.get_nowait()
            except queue.Empty:
                return

    # ─── поток распознавания ───
    def _publish_partial(self, text: str) -> None:
        if text != self._last_partial:
            self._last_partial = text
            self.on_partial(text)

    def _deliver(self, text: str) -> None:
        with self._lock:
            if self._probing:  # идёт проверка микрофона: сказанное — не команда
                self._wake_heard = False
                return
            command, self._command = self._command, False
            continuous = self._continuous
        wake_heard, self._wake_heard = self._wake_heard, False
        if command:
            self.on_listening(False)
            self.on_final(text, "command", wake_heard)
        elif continuous:
            self.on_final(text, "wake", wake_heard)

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
            if self._command_peak < 0.02 and not self._quiet_reported and self._peak_since_open > 0:
                self._quiet_reported = True
                self.on_error("Микрофон слышит вас очень тихо — команда не распознана.",
                              "Говорите ближе к микрофону или увеличьте «Чувствительность» в «Настройки → Голос».")
        elif forced and self._recognizer is not None:
            text = json.loads(self._recognizer.FinalResult()).get("text", "").strip()
            self._publish_partial("")
            if text:
                self._deliver(text)
            else:
                self.cancel_command()

    def _process_audio(self, data: bytes, now: float) -> bytes:
        samples = np.frombuffer(data, dtype=np.int16)
        gain = float(self.config.get("voice.input_gain", 1.0) or 1.0)
        if gain != 1.0:
            samples = np.clip(samples.astype(np.float32) * gain, -32768, 32767).astype(np.int16)
            data = samples.tobytes()
        if samples.size:
            peak = int(np.abs(samples.astype(np.int32)).max())
            self._peak_since_open = max(self._peak_since_open, peak)
            rms = float(np.sqrt(np.mean((samples.astype(np.float32) / 32768) ** 2)))
            self.level = max(level_from_rms(rms), self.level * 0.75)
            if self._command:
                self._command_peak = max(self._command_peak, peak / 32768)
        if (not self._silence_reported and self._peak_since_open == 0 and self._opened_at
                and now - self._opened_at > SILENCE_SECONDS):
            self._silence_reported = True
            log.warning("Микрофон %s передаёт абсолютную тишину", self.device_name)
            self.on_error(f"Микрофон «{self.device_name}» передаёт полную тишину (одни нули) — похоже, Джарвис "
                          "вас не слышит.", PRIVACY_HINT)
        return data

    def _check_partial_wake(self, partial: str) -> None:
        if self._wake_heard or self._command or not self._continuous or self._probing or not partial:
            return
        found, _rest = find_wake_word(partial, self.config.get("voice.wake_words", ["джарвис"]))
        if found:
            self._wake_heard = True
            self.on_wake()

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                needed = self._continuous or self._command or self._probing > 0
            if self._restart:
                self._restart = False
                if self._stream is not None:
                    self._close_stream()
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
                self.level *= 0.75
            if self._reset:
                self._reset = False
                self._drain()
                if self._recognizer is not None:
                    self._recognizer.Reset()
                self._wake_heard = False
                self._publish_partial("")
                continue
            now = time.monotonic()
            if data is not None and self._recognizer is not None:
                data = self._process_audio(data, now)
                for tap in list(self._taps):
                    tap.put(data)
                try:
                    if self._recognizer.AcceptWaveform(data):
                        text = json.loads(self._recognizer.Result()).get("text", "").strip()
                        self._publish_partial("")
                        if text:
                            self._deliver(text)
                        else:
                            self._wake_heard = False
                    else:
                        partial = json.loads(self._recognizer.PartialResult()).get("partial", "").strip()
                        if partial and not self._speech_started:
                            self._speech_started = now
                        self._publish_partial(partial)
                        self._check_partial_wake(partial)
                except Exception as exc:
                    log.exception("Ошибка распознавания")
                    self.on_error(f"Ошибка распознавания речи: {exc}", "")
                    self._reset = True
            self._check_timeouts(now)
        self._close_stream()
