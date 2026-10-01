"""Синтез речи Silero TTS v4 (ru) и очередь воспроизведения с мгновенной остановкой."""

from __future__ import annotations

import io
import logging
import queue
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

from .downloads import ensure_file
from .text_utils import normalize, prepare_for_speech

log = logging.getLogger(__name__)

_END = object()  # маркер «текст целиком озвучен»


class SpeechError(Exception):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


class Speaker:
    """Озвучивает текст по предложениям: синтез следующего идёт, пока играет предыдущее."""

    def __init__(self, config, on_speaking: Callable[[bool], None], on_error: Callable[[str, str], None]):
        self.config = config
        self.on_speaking = on_speaking
        self.on_error = on_error
        self.available = False
        self.speaking = False
        self.level = 0.0  # громкость текущего звука 0…1 — для анимации
        self.voices: list[str] = []
        self._model = None
        self._torch = None
        self._text: queue.Queue = queue.Queue()
        self._audio: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._generation = 0
        self._pending = 0
        self._recent: deque[tuple[float, set[str]]] = deque(maxlen=30)
        self.speaker = str(config.get("tts.speaker", "aidar"))
        self.sample_rate = int(config.get("tts.sample_rate", 48000))
        if self.sample_rate not in (8000, 24000, 48000):
            self.sample_rate = 48000

    # ─── загрузка ───
    def load(self, status: Callable[[str], None] = lambda text: None) -> None:
        try:
            import sounddevice  # noqa: F401
            import torch
        except ImportError as exc:
            raise SpeechError(f"Не установлена библиотека для синтеза речи: {exc.name}.",
                              "Установите PyTorch: pip install torch --index-url https://download.pytorch.org/whl/cpu") from None
        except OSError as exc:
            raise SpeechError(f"Не удалось загрузить аудиобиблиотеку: {exc}") from None
        self._torch = torch
        torch.set_num_threads(max(1, int(self.config.get("tts.threads", 4))))
        path = self.config.resolve_path(self.config.get("tts.model_path"))
        if path is None:
            raise SpeechError("Не указан tts.model_path в config.yaml.")
        if not path.exists():
            url = str(self.config.get("tts.model_url"))
            status("скачиваю модель синтеза речи (≈40 МБ)")
            try:
                ensure_file(url, path, lambda p: status(f"скачиваю модель синтеза речи: {p}%"))
            except Exception as exc:
                raise SpeechError(f"Не удалось скачать модель Silero: {exc}",
                                  f"Скачайте вручную {url} и положите в {path} (или запустите python download_models.py).") from None
        status("загружаю синтез речи")
        try:
            # Читаем файл средствами Python: так не мешает кириллица в пути к папке.
            importer = torch.package.PackageImporter(io.BytesIO(path.read_bytes()))
            model = importer.load_pickle("tts_models", "model")
            model.to(torch.device("cpu"))
        except Exception as exc:
            raise SpeechError(f"Не удалось загрузить модель Silero ({path}): {exc}",
                              "Удалите файл модели — при следующем запуске он скачается заново.") from None
        speakers = [v for v in (getattr(model, "speakers", []) or []) if v != "random"]
        self.voices = speakers
        if speakers and self.speaker not in speakers:
            log.warning("Голоса %s нет в модели, использую aidar. Доступны: %s", self.speaker, speakers)
            self.speaker = "aidar"
        self._model = model
        self._synthesize("Система готова.")  # прогрев: первый синтез заметно медленнее
        self.available = True
        threading.Thread(target=self._synth_loop, name="tts-synth", daemon=True).start()
        threading.Thread(target=self._play_loop, name="tts-play", daemon=True).start()

    # ─── управление ───
    def say(self, text: str) -> None:
        if not self.available or not text or not text.strip():
            return
        with self._lock:
            self._pending += 1
            generation = self._generation
        self._text.put((generation, text))

    def set_voice(self, name: str) -> None:
        if not self.voices or name in self.voices:
            self.speaker = name

    def stop(self) -> None:
        """Прерывает речь немедленно и очищает очередь."""
        with self._lock:
            self._generation += 1
            self._pending = 0
        for q in (self._text, self._audio):
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._set_speaking(False)

    @property
    def busy(self) -> bool:
        return self._pending > 0

    def is_echo(self, heard: str, window: float = 30.0) -> bool:
        """Похоже ли услышанное на то, что Джарвис сам недавно произносил (микрофон слышит динамики)."""
        words = normalize(heard).split()
        if not words:
            return False
        cutoff = time.monotonic() - window
        for moment, spoken in list(self._recent):
            if moment >= cutoff and sum(w in spoken for w in words) / len(words) >= 0.6:
                return True
        return False

    def earcon(self, kind: str = "start") -> None:
        """Короткий сигнал «слушаю» — в отдельном потоке, не мешает речи."""
        if not self.config.get("voice.earcons", True):
            return
        threading.Thread(target=self._play_earcon, args=(kind,), name="earcon", daemon=True).start()

    # ─── внутреннее ───
    def _set_speaking(self, value: bool) -> None:
        if self.speaking != value:
            self.speaking = value
            try:
                self.on_speaking(value)
            except Exception:
                log.exception("Ошибка в обработчике состояния речи")

    def _synthesize(self, text: str):
        with self._torch.no_grad():
            audio = self._model.apply_tts(text=text, speaker=self.speaker, sample_rate=self.sample_rate,
                                          put_accent=True, put_yo=True)
        return audio.numpy().astype(np.float32)

    def _synth_loop(self) -> None:
        max_chars = int(self.config.get("tts.max_chunk_chars", 800))
        while True:
            generation, text = self._text.get()
            try:
                if generation != self._generation:
                    continue
                for chunk in prepare_for_speech(text, max_chars):
                    if generation != self._generation:
                        break
                    self._recent.append((time.monotonic(), set(normalize(chunk).split())))
                    try:
                        audio = self._synthesize(chunk)
                    except Exception as exc:
                        log.warning("Silero не смог озвучить %r: %s", chunk[:80], exc)
                        continue
                    if generation == self._generation:
                        self._audio.put((generation, audio))
            finally:
                self._audio.put((generation, _END))

    def _play_loop(self) -> None:
        while True:
            generation, audio = self._audio.get()
            if audio is _END:
                with self._lock:
                    if generation != self._generation:
                        continue
                    self._pending = max(0, self._pending - 1)
                    finished = self._pending == 0
                if finished and self._audio.empty():
                    self._set_speaking(False)
                continue
            if generation != self._generation:
                continue
            self._set_speaking(True)
            self._play(audio, generation)

    def _output_device(self):
        device = self.config.get("tts.output_device")
        return None if device in (None, "") else device

    def _play(self, audio: np.ndarray, generation: int) -> None:
        import sounddevice as sd

        volume = min(2.0, max(0.1, float(self.config.get("tts.volume", 1.0))))
        data = np.clip(audio * volume, -1.0, 1.0).reshape(-1, 1)
        block = self.sample_rate // 20  # 50 мс — стоп срабатывает почти мгновенно
        try:
            stream = sd.OutputStream(samplerate=self.sample_rate, channels=1, dtype="float32",
                                     device=self._output_device(), latency="high")
            stream.start()
        except Exception as exc:
            self.stop()
            self.on_error(f"Не удалось воспроизвести звук: {exc}", "Проверьте динамики/наушники и tts.output_device.")
            return
        try:
            for start in range(0, len(data), block):
                if generation != self._generation:
                    stream.abort()
                    return
                piece = data[start:start + block]
                rms = float(np.sqrt(np.mean(piece ** 2))) if piece.size else 0.0
                self.level = min(1.0, rms * 4)
                stream.write(piece)
            stream.stop()  # дожидаемся, пока доиграет буфер
        except Exception as exc:
            log.warning("Ошибка воспроизведения: %s", exc)
        finally:
            self.level = 0.0
            stream.close()

    def _play_earcon(self, kind: str) -> None:
        try:
            import sounddevice as sd

            rate = 48000
            tones = (660, 880) if kind == "start" else (880, 660)
            parts = []
            for freq in tones:
                t = np.arange(int(rate * 0.07)) / rate
                envelope = np.sin(np.pi * t / t[-1])
                parts.append(0.18 * np.sin(2 * np.pi * freq * t) * envelope)
            sd.play(np.concatenate(parts).astype(np.float32), rate, device=self._output_device())
        except Exception as exc:
            log.debug("Сигнал не проигран: %s", exc)
