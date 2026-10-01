"""Скачивание моделей и сертификата: используется и программой при запуске, и download_models.py."""

from __future__ import annotations

import hashlib
import shutil
import ssl
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable

Progress = Callable[[int], None]

CERT_URLS = [
    "https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
    "http://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt",
]
# Отпечаток SHA-256 сертификата «Russian Trusted Root CA» (Минцифры России).
# Файл принимается, только если отпечаток совпал, поэтому способ скачивания не важен.
CERT_SHA256 = "D26D2D0231B7C39F92CC738512BA54103519E4405D68B5BD703E9788CA8ECF31"


def download_file(url: str, target: Path, progress: Progress | None = None) -> None:
    """Скачивает файл во временный *.part и переименовывает по завершении."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "Jarvis-assistant"})
    with urllib.request.urlopen(request, timeout=60) as response, open(temp, "wb") as file:
        total = int(response.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while True:
            block = response.read(1 << 16)
            if not block:
                break
            file.write(block)
            done += len(block)
            if progress and total:
                percent = done * 100 // total
                if percent != last:
                    last = percent
                    progress(percent)
    temp.replace(target)


def vosk_model_ready(path: Path | None) -> bool:
    return bool(path and path.is_dir() and (path / "am").is_dir())


def ensure_vosk_model(url: str, target: Path, progress: Progress | None = None) -> bool:
    """Скачивает и распаковывает модель Vosk, если её ещё нет. True — модель скачана сейчас."""
    if vosk_model_ready(target):
        return False
    archive = target.parent / (target.name + ".zip")
    download_file(url, archive, progress)
    with zipfile.ZipFile(archive) as zf:
        top = zf.namelist()[0].split("/")[0]
        zf.extractall(target.parent)
    extracted = target.parent / top
    if extracted != target:
        if target.exists():
            shutil.rmtree(target)
        extracted.rename(target)
    archive.unlink(missing_ok=True)
    if not vosk_model_ready(target):
        raise OSError(f"в архиве {url} нет модели Vosk")
    return True


def ensure_file(url: str, target: Path, progress: Progress | None = None) -> bool:
    if target.is_file():
        return False
    download_file(url, target, progress)
    return True


def ensure_certificate(target: Path) -> bool:
    """Скачивает корневой сертификат НУЦ Минцифры и проверяет его отпечаток."""
    if target.is_file():
        return False
    errors = []
    for url in CERT_URLS:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "Jarvis-assistant"})
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
            pem = data.decode("ascii") if b"BEGIN CERTIFICATE" in data else ssl.DER_cert_to_PEM_cert(data)
            fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
        except Exception as exc:
            errors.append(f"{url}: {exc}")
            continue
        if fingerprint != CERT_SHA256:
            errors.append(f"{url}: отпечаток не совпал ({fingerprint})")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(pem, encoding="ascii")
        return True
    raise OSError("; ".join(errors) or "сертификат не скачан")
