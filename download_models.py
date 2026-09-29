"""Скачивает всё, что нужно Джарвису для работы:

  • модель распознавания речи Vosk (vosk-model-small-ru-0.22, ~45 МБ);
  • модель синтеза речи Silero v4 ru (~40 МБ);
  • корневой сертификат НУЦ Минцифры для GigaChat (проверяется по отпечатку SHA-256).

Запуск:  python download_models.py            — всё сразу
         python download_models.py --vosk     — только Vosk
         python download_models.py --silero   — только Silero
         python download_models.py --cert     — только сертификат GigaChat
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import ssl
import sys
import urllib.request
import zipfile
from pathlib import Path

from jarvis.config import load_config

VOSK_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
CERT_URLS = [
    "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
    "http://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
]
# Отпечаток SHA-256 сертификата «Russian Trusted Root CA» (Минцифры России).
# Файл принимается, только если отпечаток совпал, поэтому способ скачивания не важен.
CERT_SHA256 = "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"


def download(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "Jarvis-assistant"})
    with urllib.request.urlopen(request, timeout=60) as response, open(temp, "wb") as file:
        total = int(response.headers.get("Content-Length") or 0)
        done = 0
        while True:
            block = response.read(1 << 16)
            if not block:
                break
            file.write(block)
            done += len(block)
            if total:
                print(f"\r  {done * 100 // total:3d}%  {done / 2**20:6.1f} из {total / 2**20:.1f} МБ", end="", flush=True)
    print()
    temp.replace(target)


def get_vosk(config) -> None:
    target = config.resolve_path(config.get("voice.vosk_model_path"))
    if target.is_dir() and any(target.iterdir()):
        print(f"✓ Vosk уже есть: {target}")
        return
    archive = target.parent / "vosk-model-small-ru-0.22.zip"
    print(f"↓ Скачиваю модель Vosk: {VOSK_URL}")
    download(VOSK_URL, archive)
    print("  Распаковываю…")
    with zipfile.ZipFile(archive) as zf:
        top = zf.namelist()[0].split("/")[0]
        zf.extractall(target.parent)
    extracted = target.parent / top
    if extracted != target:
        if target.exists():
            shutil.rmtree(target)
        extracted.rename(target)
    archive.unlink(missing_ok=True)
    print(f"✓ Vosk готов: {target}")


def get_silero(config) -> None:
    target = config.resolve_path(config.get("tts.model_path"))
    if target.is_file():
        print(f"✓ Silero уже есть: {target}")
        return
    url = str(config.get("tts.model_url"))
    print(f"↓ Скачиваю модель Silero: {url}")
    download(url, target)
    print(f"✓ Silero готов: {target}")


def get_certificate(config) -> bool:
    target = config.resolve_path(config.get("gigachat.ca_bundle_file") or "certs/russian_trusted_root_ca.crt")
    if target.is_file():
        print(f"✓ Сертификат GigaChat уже есть: {target}")
        return True
    for url in CERT_URLS:
        print(f"↓ Скачиваю сертификат НУЦ Минцифры: {url}")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Jarvis"}), timeout=30) as r:
                data = r.read()
            pem = data.decode("ascii") if b"BEGIN CERTIFICATE" in data else ssl.DER_cert_to_PEM_cert(data)
            fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
        except Exception as exc:
            print(f"  не получилось: {exc}")
            continue
        if fingerprint != CERT_SHA256:
            print(f"  ✗ отпечаток не совпал ({fingerprint}) — файл отклонён")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(pem, encoding="ascii")
        print(f"✓ Сертификат проверен и сохранён: {target}")
        return True
    print("✗ Сертификат скачать не удалось. Установите его вручную с https://www.gosuslugi.ru/crt")
    return False


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
    steps = [(args.vosk, get_vosk), (args.silero, get_silero), (args.cert, get_certificate)]
    for selected, step in steps:
        if everything or selected:
            try:
                if step(config) is False:
                    ok = False
            except Exception as exc:
                ok = False
                print(f"✗ Ошибка: {exc}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
