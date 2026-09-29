import os

from jarvis import stt, winapi


class FakeVosk:
    def __init__(self):
        self.calls = []

    def Model(self, path):  # noqa: N802 — как в библиотеке vosk
        self.calls.append((path, os.getcwd()))
        return "model"


def test_cyrillic_path_on_windows_uses_relative_name(tmp_path, monkeypatch):
    folder = tmp_path / "Иван" / "models" / "vosk-model-small-ru-0.22"
    folder.mkdir(parents=True)
    monkeypatch.setattr(winapi, "IS_WINDOWS", True)
    monkeypatch.setattr(winapi, "short_path", lambda p: p)  # коротких имён 8.3 нет
    fake = FakeVosk()
    before = os.getcwd()
    assert stt._load_vosk_model(fake, folder) == "model"
    assert fake.calls == [("vosk-model-small-ru-0.22", str(folder.parent))]
    assert os.getcwd() == before


def test_short_ascii_path_is_preferred(tmp_path, monkeypatch):
    folder = tmp_path / "Иван" / "vosk"
    folder.mkdir(parents=True)
    monkeypatch.setattr(winapi, "IS_WINDOWS", True)
    monkeypatch.setattr(winapi, "short_path", lambda p: "C:\\Users\\7E5D~1\\vosk")
    fake = FakeVosk()
    stt._load_vosk_model(fake, folder)
    assert fake.calls[0][0] == "C:\\Users\\7E5D~1\\vosk"


def test_ascii_path_loaded_directly(tmp_path):
    fake = FakeVosk()
    stt._load_vosk_model(fake, tmp_path)
    assert fake.calls[0][0] == str(tmp_path)
