import json
import shutil

from jarvis.config import ROOT_DIR, load_config, mask_secret

from .conftest import ROOT


def test_real_config_parses(tmp_path):
    config = load_config(ROOT / "config.yaml", env_path=tmp_path / ".env", user_path=None)
    assert config.load_errors == []
    assert config.get("llm.backend") in ("ollama", "gigachat", "gemini")
    assert config.get("ollama.model") == "qwen3:8b"
    assert config.get("ollama.think") is False
    assert config.get("llm.max_steps") == 5 and config.get("llm.history_messages") == 20
    assert config.get("tools.allow_powershell") is False
    assert config.get("tools.confirm_timeout") == 20
    assert config.get("hotkey") == "ctrl+alt+j"
    assert config.get("tts.speaker") == "aidar"
    assert config.get("voice.jarvis_mode") is True
    assert "джарвис" in config.get("voice.wake_words")
    prompt = config.get("system_prompt")
    assert "сэр" in prompt and "доступ" in prompt
    assert "браузер, browser, интернет" in config.get("tools.apps")


def test_window_settings_go_to_user_file(tmp_path):
    path = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", path)
    before = path.read_text(encoding="utf-8")
    user = tmp_path / "user_settings.json"
    config = load_config(path, env_path=tmp_path / ".env", user_path=user)
    config.set("llm.backend", "gemini", persist=True)
    config.set("voice.jarvis_mode", False, persist=True)
    config.set("voice.input_device", "Микрофон (USB Audio)", persist=True)
    config.set("voice.wake_words", ["джарвис", "жарвис"], persist=True)
    config.set("ollama.model", "qwen3:4b")  # без persist — только в памяти
    assert config.get("llm.backend") == "gemini" and config.get("ollama.model") == "qwen3:4b"
    # config.yaml не меняется: обновление программы не конфликтует с настройками пользователя
    assert path.read_text(encoding="utf-8") == before
    saved = json.loads(user.read_text(encoding="utf-8"))
    assert saved == {"llm": {"backend": "gemini"},
                     "voice": {"jarvis_mode": False, "input_device": "Микрофон (USB Audio)",
                               "wake_words": ["джарвис", "жарвис"]}}

    again = load_config(path, env_path=tmp_path / ".env", user_path=user)
    assert again.get("llm.backend") == "gemini"
    assert again.get("voice.jarvis_mode") is False
    assert again.get("voice.input_device") == "Микрофон (USB Audio)"
    assert again.get("voice.wake_words") == ["джарвис", "жарвис"]
    assert again.get("ollama.model") == "qwen3:8b"
    assert again.get("voice.stop_words")  # остальное — из config.yaml


def test_reset_value_to_default(tmp_path):
    user = tmp_path / "user_settings.json"
    config = load_config(ROOT / "config.yaml", env_path=tmp_path / ".env", user_path=user)
    config.set("voice.input_device", "USB", persist=True)
    config.set("voice.input_device", None, persist=True)
    assert load_config(ROOT / "config.yaml", env_path=tmp_path / ".env", user_path=user).get(
        "voice.input_device") is None


def test_broken_user_settings_are_ignored(tmp_path):
    user = tmp_path / "user_settings.json"
    user.write_text("{не json", encoding="utf-8")
    config = load_config(ROOT / "config.yaml", env_path=tmp_path / ".env", user_path=user)
    assert any("user_settings.json" in error for error in config.load_errors)
    assert config.get("ollama.model") == "qwen3:8b"
    config.set("llm.backend", "gigachat", persist=True)  # файл перезаписывается корректным
    assert json.loads(user.read_text(encoding="utf-8")) == {"llm": {"backend": "gigachat"}}


def test_unknown_backend_falls_back(tmp_path):
    user = tmp_path / "user_settings.json"
    user.write_text('{"llm": {"backend": "chatgpt"}}', encoding="utf-8")
    config = load_config(ROOT / "config.yaml", env_path=tmp_path / ".env", user_path=user)
    assert config.get("llm.backend") == "ollama"
    assert any("chatgpt" in error for error in config.load_errors)


def test_broken_yaml_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm: [unclosed\n", encoding="utf-8")
    config = load_config(path, env_path=tmp_path / ".env", user_path=None)
    assert config.load_errors and "строка" in config.load_errors[0]
    assert config.get("ollama.model") == "qwen3:8b"
    assert config.get("voice.jarvis_mode") is True


def test_bom_is_accepted(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes("﻿llm:\n  backend: gigachat\n".encode("utf-8"))
    assert load_config(path, env_path=tmp_path / ".env", user_path=None).get("llm.backend") == "gigachat"


def test_paths_and_secret_mask(tmp_path, monkeypatch):
    config = load_config(ROOT / "config.yaml", env_path=tmp_path / ".env", user_path=None)
    assert config.resolve_path("models/vosk") == ROOT_DIR / "models" / "vosk"
    assert config.resolve_path(str(tmp_path)) == tmp_path
    monkeypatch.setenv("JARVIS_TEST_DIR", str(tmp_path))
    assert config.resolve_path("$JARVIS_TEST_DIR/models") == tmp_path / "models"
    assert config.resolve_path("") is None
    assert mask_secret("sk-bl-abcdef1234567890") == "sk-b…7890"
    assert mask_secret("short") == "••••" and mask_secret("") == "" and mask_secret(None) == ""
