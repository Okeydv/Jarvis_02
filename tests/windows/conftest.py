"""Общее для проверок на настоящей Windows.

Эти тесты по-настоящему открывают и закрывают программы, печатают текст и жмут клавиши,
поэтому запускаются только в GitHub Actions (переменная JARVIS_WINDOWS_E2E=1) — не на
вашем рабочем компьютере.
"""

import os
import sys
import time
from pathlib import Path

import psutil
import pytest

from jarvis import winapi
from jarvis.tools import ToolRegistry, ToolServices

ENABLED = sys.platform == "win32" and os.environ.get("JARVIS_WINDOWS_E2E") == "1"
ARTIFACTS = Path(os.environ.get("JARVIS_ARTIFACTS", Path(__file__).resolve().parents[2] / "ci_artifacts"))


def pytest_collection_modifyitems(config, items):
    if ENABLED:
        return
    skip = pytest.mark.skip(reason="проверка на настоящей Windows: только в CI с JARVIS_WINDOWS_E2E=1")
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(skip)


def wait_for(condition, timeout: float = 10.0, step: float = 0.1):
    end = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() > end:
            return value
        time.sleep(step)


def processes(name: str) -> list[psutil.Process]:
    name = name.lower()
    return [p for p in psutil.process_iter(["name"]) if (p.info.get("name") or "").lower() == name]


def kill_all(name: str) -> None:
    found = processes(name)
    for proc in found:
        try:
            proc.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(found, timeout=5)


class Confirmer:
    """Отвечает на запросы подтверждения вместо пользователя и запоминает их."""

    def __init__(self):
        self.answer = True
        self.asked = []

    def __call__(self, confirmation):
        self.asked.append(confirmation)
        return self.answer


@pytest.fixture
def confirmer():
    return Confirmer()


@pytest.fixture
def notifications():
    return []


@pytest.fixture
def registry(config, confirmer, notifications):
    services = ToolServices(config, confirm=confirmer, focus=winapi.FocusTracker(),
                            notify=lambda title, text: notifications.append((title, text)))
    yield ToolRegistry(services)
    for timer in services.timers:
        timer.cancel()


@pytest.fixture
def artifacts():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS
