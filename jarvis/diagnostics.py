"""Диагностика: что работает, а что нет. Отчёт показывается в окне/консоли и сохраняется
в logs/diagnostics.txt, чтобы его можно было приложить к вопросу."""

from __future__ import annotations

import importlib.metadata
import platform
import struct
import sys
import time
from datetime import datetime
from typing import Callable

from . import winapi
from .config import ROOT_DIR
from .text_utils import find_wake_word

OK, WARN, FAIL = "✓", "⚠", "✗"
PACKAGES = ["customtkinter", "ollama", "gigachat", "google-genai", "vosk", "sounddevice", "numpy", "torch",
            "num2words", "psutil", "pycaw", "comtypes", "pyautogui", "Pillow", "PyYAML", "python-dotenv"]
REPORT_PATH = ROOT_DIR / "logs" / "diagnostics.txt"


class Report:
    def __init__(self, emit: Callable[[str], None]):
        self.emit = emit
        self.lines: list[str] = []

    def add(self, line: str) -> None:
        self.lines.append(line)
        self.emit(line)

    def section(self, title: str) -> None:
        self.add("")
        self.add(f"── {title} ──")

    def text(self) -> str:
        return "\n".join(self.lines)


def _db(value: float) -> str:
    import math

    return f"{20 * math.log10(value):.0f} дБ" if value > 0 else "−∞ дБ"


def check_system(report: Report, extra: list[str] | None = None) -> None:
    report.section("Система")
    report.add(f"{OK} {platform.platform()}, Python {platform.python_version()} ({struct.calcsize('P') * 8} бит)")
    report.add(f"{OK} Папка программы: {ROOT_DIR}")
    if not winapi.IS_WINDOWS:
        report.add(f"{WARN} Это не Windows — управление ПК и горячая клавиша работать не будут")
    for line in extra or []:
        report.add(line)


def check_packages(report: Report) -> None:
    report.section("Библиотеки")
    missing = []
    found = []
    for name in PACKAGES:
        try:
            found.append(f"{name} {importlib.metadata.version(name)}")
        except importlib.metadata.PackageNotFoundError:
            missing.append(name)
    report.add(f"{OK} " + ", ".join(found))
    if missing:
        report.add(f"{FAIL} Не установлены: {', '.join(missing)} — выполните install.bat или "
                   "pip install -r requirements.txt")


def check_audio_devices(report: Report, config) -> None:
    report.section("Аудиоустройства")
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        apis = sd.query_hostapis()
    except Exception as exc:
        report.add(f"{FAIL} Звуковая подсистема недоступна: {exc}")
        return
    report.add(f"{OK} PortAudio: {sd.get_portaudio_version()[1]}")
    default_in, default_out = sd.default.device
    inputs = [(i, d) for i, d in enumerate(devices) if d.get("max_input_channels", 0) > 0]
    outputs = [(i, d) for i, d in enumerate(devices) if d.get("max_output_channels", 0) > 0]
    name = lambda i: devices[i]["name"] if isinstance(i, int) and 0 <= i < len(devices) else "нет"  # noqa: E731
    report.add(f"{OK if inputs else FAIL} Микрофонов: {len(inputs)}; по умолчанию: {name(default_in)}")
    report.add(f"{OK if outputs else FAIL} Устройств вывода: {len(outputs)}; по умолчанию: {name(default_out)}")
    for index, info in inputs[:12]:
        api = apis[info["hostapi"]]["name"] if info["hostapi"] < len(apis) else "?"
        report.add(f"   [{index}] {info['name']} ({api}, {int(info['default_samplerate'])} Гц)")
    chosen = config.get("voice.input_device")
    report.add(f"{OK} В настройках выбран микрофон: {chosen if chosen not in (None, '') else 'по умолчанию'}")


def check_microphone(report: Report, config, listener=None, seconds: float = 4.0,
                     prompt: Callable[[str], None] | None = None) -> None:
    report.section("Микрофон и распознавание речи")
    from .stt import PRIVACY_HINT, Listener, VoiceError

    own = None
    if listener is None or not listener.available:
        own = Listener(config, on_partial=lambda t: None, on_final=lambda *a: None, on_listening=lambda a: None,
                       on_error=lambda m, h: report.add(f"{WARN} {m}"))
        try:
            started = time.monotonic()
            own.load(status=lambda text: None)
            report.add(f"{OK} Модель Vosk загружена за {time.monotonic() - started:.1f} с")
        except VoiceError as exc:
            report.add(f"{FAIL} {exc.message} {exc.hint}".rstrip())
            return
        listener = own
    else:
        report.add(f"{OK} Модель Vosk загружена")
    if prompt:
        prompt(f"Скажите в микрофон: «Джарвис, проверка связи» (запись {seconds:.0f} с)…")
    try:
        result = listener.probe(seconds)
    except VoiceError as exc:
        report.add(f"{FAIL} {exc.message} {exc.hint}".rstrip())
        return
    finally:
        if own is not None:
            own.shutdown()
    device = result.get("device") or "?"
    if result.get("no_audio"):
        report.add(f"{FAIL} Микрофон «{device}» не передал ни одного отсчёта. {PRIVACY_HINT}")
        return
    report.add(f"{OK} Микрофон: {device}, {result['rate']} Гц, записано {result['seconds']:.1f} с")
    if result.get("silent"):
        report.add(f"{FAIL} Полная тишина (нули) — {PRIVACY_HINT}")
        return
    loud = result["rms_db"] > -45
    report.add(f"{OK if loud else WARN} Уровень: пик {_db(result['peak'])}, средний {result['rms_db']:.0f} дБ"
               + ("" if loud else " — очень тихо: говорите ближе или увеличьте чувствительность в настройках"))
    text = result.get("text") or ""
    report.add(f"{OK if text else WARN} Распознано: «{text}»" if text else f"{WARN} Ничего не распознано")
    if text:
        wake, _ = find_wake_word(text, config.get("voice.wake_words", ["джарвис"]))
        report.add(f"{OK} Слово «Джарвис» услышано" if wake else
                   f"{WARN} Слово «Джарвис» не распознано — скажите его чётче или добавьте вариант в «Слово-активатор»")


def check_speech(report: Report, config, speaker=None, play: bool = True) -> None:
    report.section("Синтез речи")
    from .tts import Speaker, SpeechError

    if speaker is None or not speaker.available:
        speaker = Speaker(config, on_speaking=lambda v: None, on_error=lambda m, h: report.add(f"{WARN} {m}"))
        try:
            started = time.monotonic()
            speaker.load(status=lambda text: None)
            report.add(f"{OK} Модель Silero загружена за {time.monotonic() - started:.1f} с")
        except SpeechError as exc:
            report.add(f"{FAIL} {exc.message} {exc.hint}".rstrip())
            return
    started = time.monotonic()
    try:
        audio = speaker._synthesize("Проверка синтеза речи.")
    except Exception as exc:
        report.add(f"{FAIL} Синтез не удался: {exc}")
        return
    report.add(f"{OK} Голос {speaker.speaker}: фраза {len(audio) / speaker.sample_rate:.1f} с синтезирована за "
               f"{time.monotonic() - started:.2f} с")
    if play:
        try:
            import sounddevice as sd

            sd.play(audio, speaker.sample_rate)
            sd.wait()
            report.add(f"{OK} Фраза воспроизведена на устройстве вывода по умолчанию — вы должны были её услышать")
        except Exception as exc:
            report.add(f"{FAIL} Воспроизвести звук не удалось: {exc}")


def check_backend(report: Report, config, backend=None) -> None:
    from .llm import BACKEND_TITLES, LLMError, create_backend

    name = str(config.get("llm.backend", "ollama"))
    report.section(f"Языковая модель: {BACKEND_TITLES.get(name, name)}")
    try:
        backend = backend or create_backend(name, config)
        started = time.monotonic()
        backend.check()
        report.add(f"{OK} {backend.model}: на связи ({time.monotonic() - started:.1f} с)")
    except LLMError as exc:
        report.add(f"{FAIL} {exc.message} {exc.hint}".rstrip())
        return
    except Exception as exc:
        report.add(f"{FAIL} {type(exc).__name__}: {exc}")
        return
    if name == "ollama":
        try:
            models = [m.model for m in backend.client.list().models]
            report.add(f"{OK} Модели Ollama: {', '.join(models) or 'нет'}")
        except Exception as exc:
            report.add(f"{WARN} Список моделей Ollama не получен: {exc}")


def tools_self_test(registry) -> list[str]:
    """Безопасная проверка инструментов (ничего не открывает и не меняет)."""
    lines = []
    for name, args in (("get_datetime", {}), ("system_info", {}), ("list_folder", {"path": "Документы"})):
        result = registry.execute(name, args)
        text = result.text if len(result.text) < 220 else result.text[:220] + "…"
        lines.append(f"{OK if result.ok else FAIL} {name}: {text}")
    if winapi.IS_WINDOWS:
        from .tools import _endpoint_volume, default_browser_exe, list_shortcuts

        try:
            volume = _endpoint_volume().GetMasterVolumeLevelScalar()
            lines.append(f"{OK} Громкость читается: {round(volume * 100)}%")
        except Exception as exc:
            lines.append(f"{FAIL} Громкость: {exc}")
        browser = default_browser_exe()
        lines.append(f"{OK if browser else WARN} Браузер по умолчанию: {browser or 'не определён (откроется через сайт)'}")
        try:
            lines.append(f"{OK} Ярлыков в меню «Пуск»: {len(list_shortcuts())}")
        except Exception as exc:
            lines.append(f"{FAIL} Меню «Пуск»: {exc}")
        for folder in ("Desktop", "Documents", "Downloads", "Pictures"):
            path = winapi.known_folder(folder)
            lines.append(f"{OK if path.is_dir() else WARN} Папка {folder}: {path}")
    else:
        lines.append(f"{WARN} Не Windows: проверка громкости, браузера и меню «Пуск» пропущена")
    names = [t["name"] for t in registry.schemas()]
    lines.append(f"{OK} Доступно инструментов: {len(names)} ({', '.join(names)})")
    return lines


def check_tools(report: Report, config, registry=None) -> None:
    report.section("Управление компьютером")
    if registry is None:
        from .tools import ToolRegistry, ToolServices

        registry = ToolRegistry(ToolServices(config))
    for line in tools_self_test(registry):
        report.add(line)


def run_diagnostics(config, emit: Callable[[str], None], listener=None, speaker=None, registry=None,
                    backend=None, mic_seconds: float = 4.0, play: bool = True,
                    prompt: Callable[[str], None] | None = None, extra: list[str] | None = None) -> str:
    report = Report(emit)
    report.add(f"Диагностика Джарвиса — {datetime.now():%Y-%m-%d %H:%M:%S}")
    steps = [
        lambda: check_system(report, extra),
        lambda: check_packages(report),
        lambda: check_audio_devices(report, config),
        lambda: check_microphone(report, config, listener, mic_seconds, prompt),
        lambda: check_speech(report, config, speaker, play),
        lambda: check_backend(report, config, backend),
        lambda: check_tools(report, config, registry),
    ]
    for step in steps:
        try:
            step()
        except Exception as exc:  # сбой одной проверки не должен прерывать остальные
            report.add(f"{FAIL} Сбой проверки: {type(exc).__name__}: {exc}")
    try:
        REPORT_PATH.parent.mkdir(exist_ok=True)
        REPORT_PATH.write_text(report.text(), encoding="utf-8")
        report.add("")
        report.add(f"Отчёт сохранён: {REPORT_PATH}")
    except OSError as exc:
        report.add(f"{WARN} Отчёт не сохранён: {exc}")
    return report.text()


def run_cli(config) -> int:
    """python main.py --check — диагностика в консоли."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    print("Диагностика Джарвиса. Это займёт около минуты.\n")
    text = run_diagnostics(config, emit=print, prompt=lambda message: print("\n>>> " + message + "\n"))
    return 1 if f"\n{FAIL}" in "\n" + text else 0
