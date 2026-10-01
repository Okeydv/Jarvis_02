import numpy as np

from jarvis import diagnostics, winapi
from jarvis.llm.base import LLMError
from jarvis.stt import VoiceError
from jarvis.tools import TOOLS, ToolRegistry, ToolServices


def test_tools_self_test_is_safe_and_lists_tools(config, monkeypatch, tmp_path):
    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    (tmp_path / "Documents").mkdir()
    (tmp_path / "Documents" / "отчёт.docx").write_text("x", encoding="utf-8")
    lines = diagnostics.tools_self_test(ToolRegistry(ToolServices(config)))
    assert lines[0].startswith("✓ get_datetime: Сейчас")
    assert lines[1].startswith("✓ system_info: ")
    assert lines[2].startswith("✓ list_folder: ") and "отчёт.docx" in lines[2]
    assert lines[-1].startswith(f"✓ Доступно инструментов: {len(TOOLS) - 1} ")


class BusyListener:
    available = True

    def probe(self, seconds):
        raise VoiceError("Не удалось открыть микрофон.", "Закройте программы, которые его используют.")


class SilentListener:
    available = True

    def probe(self, seconds):
        return {"device": "Микрофон (Realtek Audio)", "rate": 16000, "seconds": seconds, "peak": 0.0,
                "rms_db": -120.0, "silent": True, "no_audio": False, "text": ""}


class GoodListener:
    available = True

    def probe(self, seconds):
        return {"device": "Микрофон (USB Audio)", "rate": 48000, "seconds": seconds, "peak": 0.6,
                "rms_db": -28.0, "silent": False, "no_audio": False, "text": "джарвис проверка связи"}


class FakeSpeaker:
    available = True
    speaker = "aidar"
    sample_rate = 48000

    def _synthesize(self, text):
        return np.zeros(24000, dtype=np.float32)


class DownBackend:
    model = "qwen3:8b"

    def check(self):
        raise LLMError("Ollama не запущена.", "Запустите Ollama: ollama serve")


def _run(config, monkeypatch, tmp_path, listener):
    monkeypatch.setattr(diagnostics, "REPORT_PATH", tmp_path / "logs" / "diagnostics.txt")
    monkeypatch.setattr(winapi, "known_folder", lambda name: tmp_path / name)
    lines = []
    text = diagnostics.run_diagnostics(config, lines.append, listener=listener, speaker=FakeSpeaker(),
                                       backend=DownBackend(), play=False, mic_seconds=0.1)
    return text, lines


def test_report_continues_after_failures(config, monkeypatch, tmp_path):
    text, lines = _run(config, monkeypatch, tmp_path, BusyListener())
    assert "✗ Не удалось открыть микрофон. Закройте программы, которые его используют." in text
    assert "✓ Голос aidar: фраза 0.5 с" in text
    assert "✗ Ollama не запущена. Запустите Ollama: ollama serve" in text
    assert "── Управление компьютером ──" in text  # проверки после сбоев всё равно выполнены
    saved = (tmp_path / "logs" / "diagnostics.txt").read_text(encoding="utf-8")
    assert saved.startswith("Диагностика Джарвиса") and "Ollama не запущена" in saved
    assert lines[-1].startswith("Отчёт сохранён")


def test_silent_microphone_explains_privacy_settings(config, monkeypatch, tmp_path):
    text, _ = _run(config, monkeypatch, tmp_path, SilentListener())
    assert "✗ Полная тишина" in text and "Конфиденциальность" in text


def test_working_microphone_hears_wake_word(config, monkeypatch, tmp_path):
    text, _ = _run(config, monkeypatch, tmp_path, GoodListener())
    assert "✓ Микрофон: Микрофон (USB Audio), 48000 Гц" in text
    assert "✓ Распознано: «джарвис проверка связи»" in text
    assert "✓ Слово «Джарвис» услышано" in text
