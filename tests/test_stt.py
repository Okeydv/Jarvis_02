"""Логика распознавания без настоящего микрофона: выбор устройства, уровень, тишина, слово «Джарвис»."""

import sys
import threading
import time
import types

import numpy as np
import pytest

from jarvis import stt
from jarvis.stt import PRIVACY_HINT, Listener, level_from_rms, list_input_devices

DEVICES = [
    {"name": "Microsoft Sound Mapper - Input", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100},
    {"name": "Микрофон (Realtek Audio)", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100},
    {"name": "Микрофон (USB Audio)", "hostapi": 0, "max_input_channels": 1, "default_samplerate": 48000},
    {"name": "Динамики (Realtek Audio)", "hostapi": 0, "max_input_channels": 0, "default_samplerate": 48000},
    {"name": "Микрофон (Realtek Audio)", "hostapi": 0, "max_input_channels": 2, "default_samplerate": 44100},
    {"name": "Первичный драйвер записи звука", "hostapi": 1, "max_input_channels": 2, "default_samplerate": 44100},
    {"name": "Микрофон (USB Audio)", "hostapi": 1, "max_input_channels": 1, "default_samplerate": 48000},
    {"name": "Микрофон (USB Audio)", "hostapi": 2, "max_input_channels": 1, "default_samplerate": 48000},
    {"name": "Микрофон (USB Audio)", "hostapi": 3, "max_input_channels": 1, "default_samplerate": 48000},
]
HOSTAPIS = [{"name": "MME"}, {"name": "Windows DirectSound"}, {"name": "Windows WASAPI"}, {"name": "Windows WDM-KS"}]


@pytest.fixture
def fake_sd(monkeypatch):
    module = types.SimpleNamespace(
        query_devices=lambda device=None, kind=None: DEVICES if device is None else DEVICES[device],
        query_hostapis=lambda index=None: HOSTAPIS if index is None else HOSTAPIS[index],
        default=types.SimpleNamespace(hostapi=0, device=(1, 3)),
    )
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    return module


class Events:
    def __init__(self):
        self.errors, self.finals, self.wakes, self.listening = [], [], 0, []

    def listener(self, config):
        def wake():
            self.wakes += 1

        return Listener(config, on_partial=lambda text: None,
                        on_final=lambda *args: self.finals.append(args),
                        on_listening=self.listening.append,
                        on_error=lambda message, hint: self.errors.append((message, hint)),
                        on_wake=wake)


def test_level_scale():
    assert level_from_rms(0) == 0
    assert level_from_rms(10 ** (-60 / 20)) == pytest.approx(0)
    assert level_from_rms(10 ** (-35 / 20)) == pytest.approx(0.5)
    assert level_from_rms(10 ** (-10 / 20)) == pytest.approx(1)
    assert level_from_rms(1.0) == 1


def test_microphone_list_for_settings(fake_sd):
    devices = list_input_devices()  # только основной звуковой API, без дублей и «переназначения»
    assert [d["name"] for d in devices] == ["Микрофон (Realtek Audio)", "Микрофон (USB Audio)"]
    assert devices[0] == {"index": 1, "name": "Микрофон (Realtek Audio)", "default": True}
    assert devices[1]["default"] is False


def test_device_candidates(config, fake_sd):
    events = Events()
    listener = events.listener(config)
    # по умолчанию — системный микрофон, затем он же в других звуковых API
    assert listener._candidates() == [None, 4]

    config.set("voice.input_device", "Микрофон (USB Audio)")
    # сначала из основного звукового API, затем остальные (кроме WDM-KS), в конце — системный
    assert listener._candidates() == [2, 6, 7, None, 4]
    config.set("voice.input_device", "usb")
    assert listener._candidates() == [2, 6, 7, None, 4]
    config.set("voice.input_device", 4)
    assert listener._candidates() == [4, None]

    config.set("voice.input_device", "Гарнитура Bluetooth")
    assert listener._candidates() == [None, 4]
    assert listener._candidates() == [None, 4]
    assert len(events.errors) == 1 and "не найден" in events.errors[0][0]  # предупреждение — один раз


def test_total_silence_is_reported_once(config):
    events = Events()
    listener = events.listener(config)
    listener.device_name = "Микрофон (Realtek Audio)"
    listener._opened_at = time.monotonic() - 2
    zeros = np.zeros(1600, dtype=np.int16).tobytes()
    listener._process_audio(zeros, time.monotonic())
    assert events.errors == []  # пара секунд тишины — нормально (вы просто молчите)
    listener._opened_at = time.monotonic() - stt.SILENCE_SECONDS - 1
    zeros = np.zeros(1600, dtype=np.int16).tobytes()
    listener._process_audio(zeros, time.monotonic())
    listener._process_audio(zeros, time.monotonic())
    assert len(events.errors) == 1
    message, hint = events.errors[0]
    assert "тишину" in message and hint == PRIVACY_HINT


def test_quiet_noise_is_not_silence(config):
    events = Events()
    listener = events.listener(config)
    listener._opened_at = time.monotonic() - stt.SILENCE_SECONDS - 1
    noise = (np.random.default_rng(1).normal(0, 30, 1600)).astype(np.int16).tobytes()
    listener._process_audio(noise, time.monotonic())
    assert events.errors == [] and listener.level >= 0


def test_input_gain(config):
    events = Events()
    listener = events.listener(config)
    config.set("voice.input_gain", 3.0)
    samples = np.array([1000, -2000, 20000], dtype=np.int16)
    boosted = np.frombuffer(listener._process_audio(samples.tobytes(), time.monotonic()), dtype=np.int16)
    assert boosted.tolist() == [3000, -6000, 32767]  # с ограничением, без переполнения
    assert listener.level > 0.5


def test_wake_word_detected_in_partial_result(config):
    events = Events()
    listener = events.listener(config)
    listener._continuous = True
    listener._check_partial_wake("")
    listener._check_partial_wake("открой")
    assert events.wakes == 0
    listener._check_partial_wake("джарвис")
    listener._check_partial_wake("джарвис открой")
    assert events.wakes == 1  # один раз на фразу
    listener._deliver("джарвис открой браузер")
    assert events.finals == [("джарвис открой браузер", "wake", True)]
    listener._check_partial_wake("джарвис")
    assert events.wakes == 2  # новая фраза — снова можно


def test_wake_word_ignored_without_jarvis_mode(config):
    events = Events()
    listener = events.listener(config)
    listener._check_partial_wake("джарвис")
    listener._deliver("джарвис открой браузер")
    assert events.wakes == 0 and events.finals == []  # режим «Джарвис» выключен — фразы не нужны


def test_command_mode_delivers_and_stops_listening(config):
    events = Events()
    listener = events.listener(config)
    listener.available = True
    listener.listen_command(5)
    assert listener.command_active and events.listening == [True]
    listener._deliver("открой блокнот")
    assert events.finals == [("открой блокнот", "command", False)]
    assert events.listening == [True, False] and not listener.command_active


def test_command_timeout_without_speech(config):
    events = Events()
    listener = events.listener(config)
    listener.available = True
    listener.listen_command(0.01)
    listener._peak_since_open = 100
    listener._check_timeouts(time.monotonic() + 1)
    assert not listener.command_active and events.listening == [True, False]
    assert events.errors and "тихо" in events.errors[0][0]  # микрофон что-то слышал, но очень тихо


def test_probe_requires_loaded_model(config):
    listener = Events().listener(config)
    with pytest.raises(stt.VoiceError):
        listener.probe(0.1)


def test_microphone_test_phrase_is_not_a_command(config):
    events = Events()
    listener = events.listener(config)
    listener._continuous = True
    listener._probing = 1  # идёт «Проверить микрофон»
    listener._check_partial_wake("джарвис проверка")
    listener._deliver("джарвис проверка связи")
    assert events.wakes == 0 and events.finals == []
    listener._probing = 0
    listener._deliver("джарвис открой браузер")
    assert events.finals == [("джарвис открой браузер", "wake", False)]


def test_open_failure_is_reported_once_and_retried_slowly(config, fake_sd, monkeypatch):
    events = Events()
    listener = events.listener(config)

    def broken(*args, **kwargs):
        raise RuntimeError("Error opening RawInputStream: Unanticipated host error [MME error 1]")

    fake_sd.RawInputStream = broken
    listener._vosk = object()
    listener._continuous = True
    thread = threading.Thread(target=listener._run, daemon=True)
    thread.start()
    time.sleep(1.0)
    listener._stop.set()
    listener._wake.set()
    thread.join(5)
    assert len(events.errors) == 1 and "Не удалось открыть микрофон" in events.errors[0][0]
    assert PRIVACY_HINT in events.errors[0][1]
    assert not listener._continuous  # слушать перестал, а не долбит устройство


def test_probe_fails_fast_when_microphone_does_not_open(config):
    listener = Events().listener(config)
    listener.available = True
    listener._open_error = None

    def fake_wake():
        listener._open_error = stt.VoiceError("Не удалось открыть микрофон.", PRIVACY_HINT)

    listener._wake = types.SimpleNamespace(set=fake_wake)
    started = time.monotonic()
    with pytest.raises(stt.VoiceError):
        listener.probe(5.0)
    assert time.monotonic() - started < 1.0 and listener._probing == 0
