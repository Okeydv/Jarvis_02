import shutil

import yaml

from jarvis.config import load_config, persist_scalar

from .conftest import ROOT


def test_real_config_parses():
    config = load_config(ROOT / "config.yaml")
    assert config.load_errors == []
    assert config.get("llm.backend") in ("ollama", "gigachat", "gemini")
    assert config.get("ollama.model") == "qwen3:8b"
    assert config.get("ollama.think") is False
    assert config.get("llm.max_steps") == 5 and config.get("llm.history_messages") == 20
    assert config.get("tools.allow_powershell") is False
    assert config.get("tools.confirm_timeout") == 20
    assert config.get("hotkey") == "ctrl+alt+j"
    assert config.get("tts.speaker") == "aidar"
    assert "сэр" in config.get("system_prompt")
    assert "браузер, browser, интернет" in config.get("tools.apps")


def test_persist_keeps_comments(tmp_path):
    path = tmp_path / "config.yaml"
    shutil.copy(ROOT / "config.yaml", path)
    before = path.read_text(encoding="utf-8")
    config = load_config(path, env_path=tmp_path / ".env")
    config.set("llm.backend", "gemini", persist=True)
    config.set("voice.jarvis_mode", True, persist=True)
    config.set("voice.speak_replies", False, persist=True)
    after = path.read_text(encoding="utf-8")
    data = yaml.safe_load(after)
    assert data["llm"]["backend"] == "gemini"
    assert data["voice"]["jarvis_mode"] is True and data["voice"]["speak_replies"] is False
    # комментарии и остальное содержимое на месте
    assert after.count("#") == before.count("#")
    assert len(after.splitlines()) == len(before.splitlines())


def test_persist_only_touches_exact_key(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("a:\n  model: x   # коммент\nb:\n  model: y\n", encoding="utf-8")
    assert persist_scalar(path, ["b", "model"], "GigaChat-2-Max")
    assert path.read_text(encoding="utf-8") == "a:\n  model: x   # коммент\nb:\n  model: GigaChat-2-Max\n"
    assert not persist_scalar(path, ["c", "model"], "z")


def test_broken_yaml_falls_back_to_defaults(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("llm: [unclosed\n", encoding="utf-8")
    config = load_config(path, env_path=tmp_path / ".env")
    assert config.load_errors and "строка" in config.load_errors[0]
    assert config.get("ollama.model") == "qwen3:8b"


def test_bom_is_accepted(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes("\ufeffllm:\n  backend: gigachat\n".encode("utf-8"))
    assert load_config(path, env_path=tmp_path / ".env").get("llm.backend") == "gigachat"
