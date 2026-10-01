"""Скачивает всё, что нужно Джарвису для работы:

  • модель распознавания речи Vosk (vosk-model-small-ru-0.22, ~45 МБ);
  • модель синтеза речи Silero v4 ru (~40 МБ);
  • корневой сертификат НУЦ Минцифры для GigaChat (проверяется по отпечатку SHA-256).

Модели скачиваются и сами при первом запуске программы; этот скрипт удобен, чтобы
подготовить всё заранее.

Запуск:  python download_models.py            — всё сразу
         python download_models.py --vosk     — только Vosk
         python download_models.py --silero   — только Silero
         python download_models.py --cert     — только сертификат GigaChat
"""

from __future__ import annotations

import argparse
import sys

from jarvis.config import load_config
from jarvis.downloads import ensure_certificate, ensure_file, ensure_vosk_model


def _progress(percent: int) -> None:
    print(f"\r  {percent:3d}%", end="", flush=True)
    if percent >= 100:
        print()


def get_vosk(config) -> None:
    target = config.resolve_path(config.get("voice.vosk_model_path"))
    url = str(config.get("voice.vosk_model_url"))
    print(f"↓ Модель распознавания речи Vosk: {url}")
    downloaded = ensure_vosk_model(url, target, _progress)
    print(f"✓ Vosk {'скачан' if downloaded else 'уже есть'}: {target}")


def get_silero(config) -> None:
    target = config.resolve_path(config.get("tts.model_path"))
    url = str(config.get("tts.model_url"))
    print(f"↓ Модель синтеза речи Silero: {url}")
    downloaded = ensure_file(url, target, _progress)
    print(f"✓ Silero {'скачан' if downloaded else 'уже есть'}: {target}")


def get_certificate(config) -> None:
    target = config.resolve_path(config.get("gigachat.ca_bundle_file") or "certs/russian_trusted_root_ca.crt")
    print("↓ Корневой сертификат НУЦ Минцифры для GigaChat")
    downloaded = ensure_certificate(target)
    print(f"✓ Сертификат {'проверен и сохранён' if downloaded else 'уже есть'}: {target}")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")  # вывод в консоль с кодировкой cp866/cp1251
    parser = argparse.ArgumentParser(description="Скачать модели и сертификат для Джарвиса")
    parser.add_argument("--vosk", action="store_true", help="модель распознавания речи")
    parser.add_argument("--silero", action="store_true", help="модель синтеза речи")
    parser.add_argument("--cert", action="store_true", help="сертификат НУЦ Минцифры для GigaChat")
    args = parser.parse_args()
    everything = not (args.vosk or args.silero or args.cert)
    config = load_config()
    ok = True
    for selected, step in ((args.vosk, get_vosk), (args.silero, get_silero), (args.cert, get_certificate)):
        if everything or selected:
            try:
                step(config)
            except Exception as exc:
                ok = False
                print(f"\n✗ Ошибка: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
