"""Голос на настоящей Windows: вместо микрофона — виртуальный кабель VB-CABLE.

Фраза синтезируется Silero (голосом xenia) и проигрывается в «CABLE Input», а Джарвис слушает
«CABLE Output» — как если бы её сказали в микрофон.
"""

import sys

import pytest

if sys.platform != "win32":
    pytest.skip("проверки только для Windows", allow_module_level=True)

import threading  # noqa: E402
import time  # noqa: E402

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402
import yaml  # noqa: E402

from jarvis.config import DEFAULTS, ROOT_DIR, Config, deep_merge  # noqa: E402
from jarvis.stt import Listener, list_input_devices  # noqa: E402
from jarvis.text_utils import find_wake_word  # noqa: E402
from jarvis.tts import Speaker  # noqa: E402

from .conftest import ENABLED, wait_for  # noqa: E402


def cable_devices() -> tuple[int | None, int | None]:
    """(куда играть — «CABLE Input», откуда слушать — «CABLE Output») в MME."""
    playback = capture = None
    for index, info in enumerate(sd.query_devices()):
        if sd.query_hostapis(info["hostapi"])["name"] != "MME":
            continue
        name = info["name"].lower()
        if playback is None and "cable input" in name and info["max_output_channels"] > 0:
            playback = index
        if capture is None and "cable output" in name and info["max_input_channels"] > 0:
            capture = index
    return playback, capture


@pytest.fixture(scope="module")
def cable():
    if not ENABLED:
        pytest.skip("только в CI с JARVIS_WINDOWS_E2E=1")
    playback, capture = cable_devices()
    if playback is None or capture is None:
        pytest.skip("виртуальный кабель VB-CABLE не установлен")
    return playback, capture


@pytest.fixture(scope="module")
def voice_config():
    raw = yaml.safe_load((ROOT_DIR / "config.yaml").read_text(encoding="utf-8"))
    config = Config(deep_merge(DEFAULTS, raw), path=None)
    config.set("voice.input_device", "CABLE Output")
    return config


@pytest.fixture(scope="module")
def speaker(cable, voice_config):
    speaker = Speaker(voice_config, on_speaking=lambda value: None, on_error=lambda message, hint: None)
    speaker.load()
    return speaker


def say(speaker: Speaker, text: str, device: int) -> None:
    """Говорит фразу «в микрофон» другим голосом, чем у Джарвиса."""
    audio = speaker._model.apply_tts(text=text, speaker="xenia", sample_rate=48000).numpy()
    audio = np.concatenate([np.zeros(4800), audio * 0.8, np.zeros(19200)]).astype(np.float32)
    sd.play(audio, 48000, device=device, blocking=True)


class Events:
    def __init__(self):
        self.finals, self.errors, self.partials = [], [], []
        self.wakes = 0

    def wake(self):
        self.wakes += 1


@pytest.fixture
def listener(cable, voice_config):
    events = Events()
    listener = Listener(voice_config, on_partial=events.partials.append,
                        on_final=lambda text, mode, wake=False: events.finals.append((text, mode, wake)),
                        on_listening=lambda active: None,
                        on_error=lambda message, hint: events.errors.append(message), on_wake=events.wake)
    listener.load()
    listener.events = events
    yield listener
    listener.shutdown()


def test_cable_is_listed_as_microphone(cable):
    names = [device["name"] for device in list_input_devices()]
    assert any("CABLE Output" in name for name in names), names


def test_jarvis_mode_hears_wake_word_and_command(cable, speaker, listener):
    listener.set_continuous(True)
    assert wait_for(lambda: listener.stream_open, 10), listener.events.errors
    print("Открыт микрофон:", listener.device_name)
    assert "CABLE Output" in listener.device_name and "VB-Audio Point" not in listener.device_name
    time.sleep(1.0)
    say(speaker, "Джарвис, открой блокнот.", cable[0])
    assert wait_for(lambda: listener.events.finals, 10), (listener.events.partials[-5:], listener.events.errors)
    text, mode, wake_heard = listener.events.finals[-1]
    found, command = find_wake_word(text, listener.config.get("voice.wake_words"))
    assert mode == "wake" and found and "блокнот" in command, text
    assert listener.events.wakes >= 1 and wake_heard  # «Джарвис» узнан ещё до конца фразы
    assert listener.level >= 0 and not listener.events.errors


def test_microphone_button_command(cable, speaker, listener):
    listener.listen_command(10)
    time.sleep(0.5)
    say(speaker, "Который час?", cable[0])
    assert wait_for(lambda: listener.events.finals, 10), (listener.events.partials[-5:], listener.events.errors)
    text, mode, _ = listener.events.finals[-1]
    assert mode == "command" and "час" in text, text


def test_microphone_check_like_settings_button(cable, speaker, listener):
    result = {}
    thread = threading.Thread(target=lambda: result.update(listener.probe(5.0)))
    thread.start()
    time.sleep(0.5)
    say(speaker, "Джарвис, проверка связи.", cable[0])
    thread.join(15)
    assert result and not result["silent"] and not result["no_audio"], result
    assert result["rms_db"] > -45 and "проверка связи" in result["text"], result
